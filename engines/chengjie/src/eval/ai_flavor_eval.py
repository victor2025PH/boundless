# -*- coding: utf-8 -*-
"""出站文本「AI 味」量化评测（活人感 P1-9 核心，2026-08-03，纯函数零 IO）。

背景：语音活人感诊断的量化实锤（近 14 天 885 条出站：33% 以「哈哈」开头、
感叹词类开场 ~44%、54% 含反问、67% 带 emoji、语音条均长 56 字/p90=97）——
单条看都还行，分布看全是同一个模子，且已有客户当场说「有点机器味」。
P0 批次（生成范式/开场去重/B 线分条/reply_variety）落地后，本模块把「AI 味」
钉成可回归的数字：修好有判据、再坏有人点名（pytest 门禁 + 周报 CLI 两用）。

设计（与 media_consistency_eval / proactive_review 同族）：
  - 纯函数零 IO：语料由调用方喂（CLI 读库、门禁喂金标），本模块只算；
  - 全部确定性可解释指标——刻意不用 LLM 评委（评委自身漂移会把门禁变噪声源）；
  - AI 质疑检测词表**保守**（宁可漏报不误报——误报几次运营就不再信这个数）；
  - 验收目标 ``TARGETS`` 与 2026-08-03 诊断报告的验收表同源，
    ``target_checks`` 把验收表变成可执行判据。

与 ``voice_opener_guard`` 的口径差异（刻意）：守卫是**处置**口径——单字感叹词
必须跟分隔标点才认（防误剥「啊？你说什么」的真实反应）；本模块是**统计**口径
——多字感叹词（哈哈/哎呀）不要求分隔符也计（「哈哈你还真执着」显然是感叹开场，
统计漏计会低估病情）。单字仍要求分隔符，与守卫一致。
"""
from __future__ import annotations

import re
from bisect import bisect_right
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ── 开场词口径 ────────────────────────────────────────────────────────────────
_LEAD_QUOTES = " \t「『\"'（(【[“‘"
# 笑声家族（中文连字 + 拉丁 haha/hhh/lol），归一后分别记 哈哈/嘿嘿/... / haha
_LAUGH_RE = re.compile(r"^(哈{2,}|嘿{2,}|呵{2,}|嘻{2,})")
_LAUGH_LATIN_RE = re.compile(r"^(?i:(?:a?ha){2,}h?|h{3,}|lo+l+)\b")
# 多字话语标记：自身已具开场词性质，不要求分隔符（统计口径，见模块注释）
_MULTI_OPENERS = (
    "诶嘿", "哎呀", "哎哟", "哎呦", "嗯嗯", "好啦", "好嘞", "行行行",
    "哇塞", "天哪", "我的天", "话说", "其实", "说起来", "对了", "嗨呀",
)
_SINGLE_OPENERS = "嘿诶哇嗨唉嗯哦喔咦嚯哎"
_SINGLE_SEP = "，,、!！~～ \t"

_LAUGH_FAMILY = frozenset({"哈哈", "嘿嘿", "呵呵", "嘻嘻", "haha"})

_EMOJI_RE = re.compile(
    r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF"
    r"\uFE0F\u200D\u2764\u2B50]"
)
_SENT_END_RE = re.compile(r"[。！？!?…]")
_QUESTION_CHARS = ("？", "?")
# 客服腔信号：敬语「您」或典型客服短语（陪伴人设语境下都是穿帮词）
_SERVICE_PHRASES = ("请稍等", "马上回您", "为您", "请您", "收到，我")


def opener_of(text: str) -> str:
    """取文本的「开场词」（归一化），无 → ""。纯函数，确定性。

    笑声连字折叠（哈哈哈→哈哈，hahaha→haha）；多字话语标记不要求分隔符；
    单字感叹词要求后跟分隔标点/空白（与 voice_opener_guard 同界：
    「啊？你说什么」是真实反应不算开场词——？! 不在分隔符集内）。
    """
    s = str(text or "").lstrip(_LEAD_QUOTES)
    if not s:
        return ""
    m = _LAUGH_RE.match(s)
    if m:
        return m.group(1)[0] * 2
    if _LAUGH_LATIN_RE.match(s):
        return "haha"
    for w in _MULTI_OPENERS:
        if s.startswith(w):
            return w
    if s[0] in _SINGLE_OPENERS and len(s) > 1 and s[1] in _SINGLE_SEP:
        return s[0]
    return ""


def _percentile(sorted_vals: Sequence[float], pct: float) -> float:
    """线性插值分位数（输入须已升序；空 → 0）。"""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = k - lo
    return float(sorted_vals[lo]) * (1 - frac) + float(sorted_vals[hi]) * frac


def flavor_report(texts: Iterable[str]) -> Dict[str, Any]:
    """一批出站文本 → 「AI 味」指标 dict。纯函数。

    指标全部是比率/长度统计，n=0 时各项为 0（调用方按无样本处理）。
    """
    clean: List[str] = []
    for t in texts:
        s = str(t or "").strip()
        if s:
            clean.append(s)
    n = len(clean)
    rep: Dict[str, Any] = {
        "n": n, "laugh_opener_rate": 0.0, "interjection_opener_rate": 0.0,
        "top_openers": [], "top_opener_share": 0.0,
        "question_rate": 0.0, "question_end_rate": 0.0,
        "emoji_rate": 0.0, "tilde_rate": 0.0, "service_tone_rate": 0.0,
        "multi_sentence_rate": 0.0, "essay_rate": 0.0,
        "len_avg": 0.0, "len_p50": 0.0, "len_p90": 0.0,
    }
    if not n:
        return rep
    openers: Counter = Counter()
    laugh = q = q_end = emoji = tilde = service = multi = essay = 0
    lens: List[int] = []
    for s in clean:
        op = opener_of(s)
        if op:
            openers[op] += 1
            if op in _LAUGH_FAMILY:
                laugh += 1
        has_q = any(c in s for c in _QUESTION_CHARS)
        if has_q:
            q += 1
        tail = _EMOJI_RE.sub("", s).rstrip(" ~～")
        if tail.endswith(_QUESTION_CHARS):
            q_end += 1
        if _EMOJI_RE.search(s):
            emoji += 1
        if "~" in s or "～" in s:
            tilde += 1
        if "您" in s or any(p in s for p in _SERVICE_PHRASES):
            service += 1
        n_sent = len(_SENT_END_RE.findall(s))
        if n_sent >= 2:
            multi += 1
            if has_q:
                essay += 1        # 多句 + 带问 ＝「反应→自述→反问」作文式结构代理
        lens.append(len(s))
    lens.sort()
    top = openers.most_common(5)
    rep.update({
        "laugh_opener_rate": round(laugh / n, 4),
        "interjection_opener_rate": round(sum(openers.values()) / n, 4),
        "top_openers": [(k, v) for k, v in top],
        "top_opener_share": round((top[0][1] / n) if top else 0.0, 4),
        "question_rate": round(q / n, 4),
        "question_end_rate": round(q_end / n, 4),
        "emoji_rate": round(emoji / n, 4),
        "tilde_rate": round(tilde / n, 4),
        "service_tone_rate": round(service / n, 4),
        "multi_sentence_rate": round(multi / n, 4),
        "essay_rate": round(essay / n, 4),
        "len_avg": round(sum(lens) / n, 1),
        "len_p50": round(_percentile(lens, 50), 1),
        "len_p90": round(_percentile(lens, 90), 1),
    })
    return rep


# ── 语音镜像文本清洗 ─────────────────────────────────────────────────────────
_VOICE_PREFIX_RE = re.compile(r"^\s*\[(?:语音|voice)\]\s*", re.IGNORECASE)


def voice_mirror_text(text: str) -> str:
    """语音消息镜像文本 → 实际念稿（剥「[语音]」前缀；纯占位/过短 → ""）。"""
    s = _VOICE_PREFIX_RE.sub("", str(text or "").strip())
    # 「[语音]×3」这类计数占位剥前缀后剩 ×3 —— 非念稿
    if len(s) < 2 or s.startswith(("×", "x")):
        return ""
    return s


# ── AI 质疑检测 ──────────────────────────────────────────────────────────────
# 单一事实源在运行时侧 ``src/utils/ai_suspicion``（2026-08-03 闭环后迁移：
# 检测器同时服务线上 hint 注入与离线周报，两侧必须同口径）。此处 re-export
# 保住既有消费方（本模块 __all__ / 周报 CLI / 门禁测试）零改动。
from src.utils.ai_suspicion import detect_ai_suspicion  # noqa: E402  (re-export)


# ── 出站 episode 回复率（语音 vs 文字）──────────────────────────────────────
_VOICE_MEDIA = ("voice", "audio")


def episode_reply_stats(
    rows: Sequence[Tuple[float, str, str]],
    *,
    window_s: float = 48 * 3600.0,
    burst_gap_s: float = 1800.0,
    now: Optional[float] = None,
) -> Dict[str, Dict[str, int]]:
    """单会话消息序列 → 出站 episode 数与被回复数（按含语音/纯文字分桶）。

    ``rows``＝该会话按 ts 升序的 (ts, direction, media_type)。

    口径（为什么不逐条算）：连发 3 条后客户回 1 条，逐条口径＝3 发 1 回（回复率
    被系统性压到 1/3），且语音常整段拆条（P0-4 之后更甚）——按「连发簇」算才
    公平：间隔 ≤burst_gap_s 且中间无入站的连续出站算一个 episode，episode 内
    任一条是语音即归 voice 桶（对比语义＝「这次触达带没带语音」）；episode 结束
    后 window_s 内出现首条入站＝被回复。窗尾新 episode 可能未到回复窗（轻微
    低估），两桶同受此偏差，对比口径公平。``now`` 传入时，episode 结束距 now
    不足 window_s 的**未回复** episode 不计入分母（回复窗未走完，判未回是抢答）。
    """
    out: Dict[str, Dict[str, int]] = {
        "voice": {"episodes": 0, "replied": 0},
        "text": {"episodes": 0, "replied": 0},
    }
    if not rows:
        return out
    in_ts: List[float] = [float(r[0]) for r in rows if r[1] == "in"]
    episodes: List[Tuple[float, bool]] = []   # (end_ts, has_voice)
    cur_end: Optional[float] = None
    cur_voice = False
    for ts, direction, media in rows:
        ts = float(ts)
        if direction != "out":
            if cur_end is not None:
                episodes.append((cur_end, cur_voice))
                cur_end, cur_voice = None, False
            continue
        if cur_end is not None and (ts - cur_end) > burst_gap_s:
            episodes.append((cur_end, cur_voice))
            cur_end, cur_voice = None, False
        cur_end = ts
        cur_voice = cur_voice or (str(media or "").lower() in _VOICE_MEDIA)
    if cur_end is not None:
        episodes.append((cur_end, cur_voice))
    for end_ts, has_voice in episodes:
        idx = bisect_right(in_ts, end_ts)
        replied = idx < len(in_ts) and (in_ts[idx] - end_ts) <= window_s
        if not replied and now is not None and (float(now) - end_ts) < window_s:
            continue          # 回复窗未走完的未回 episode：不计分母
        bucket = out["voice" if has_voice else "text"]
        bucket["episodes"] += 1
        if replied:
            bucket["replied"] += 1
    return out


def merge_reply_stats(parts: Iterable[Dict[str, Dict[str, int]]]) -> Dict[str, Any]:
    """多会话 episode 统计合并 + 回复率。"""
    total: Dict[str, Dict[str, Any]] = {
        "voice": {"episodes": 0, "replied": 0},
        "text": {"episodes": 0, "replied": 0},
    }
    for p in parts:
        for k in ("voice", "text"):
            b = (p or {}).get(k) or {}
            total[k]["episodes"] += int(b.get("episodes") or 0)
            total[k]["replied"] += int(b.get("replied") or 0)
    for k in ("voice", "text"):
        n = total[k]["episodes"]
        total[k]["reply_rate"] = round(total[k]["replied"] / n, 3) if n else 0.0
    return total


# ── 验收目标（与 2026-08-03 诊断报告验收表同源）─────────────────────────────
TARGETS: Dict[str, Any] = {
    "interjection_opener_rate_max": 0.10,   # 同质化开场 < 10%
    "question_rate_band": (0.20, 0.40),     # 反问率 30%±10（有波动才真实）
    "voice_len_avg_max": 30.0,              # 语音条均长 ≤30 字
    "voice_len_p90_max": 48.0,              # 语音条 p90 ≤48 字
}


def target_checks(overall: Dict[str, Any],
                  voice: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把验收表变成可执行判据。返回 [{name, value, target, ok}]；无样本项跳过。"""
    checks: List[Dict[str, Any]] = []
    if overall.get("n"):
        v = float(overall.get("interjection_opener_rate") or 0.0)
        checks.append({
            "name": "同质化开场占比", "value": v,
            "target": f"<= {TARGETS['interjection_opener_rate_max']:.0%}",
            "ok": v <= TARGETS["interjection_opener_rate_max"],
        })
        q = float(overall.get("question_rate") or 0.0)
        lo, hi = TARGETS["question_rate_band"]
        checks.append({
            "name": "消息含反问率", "value": q,
            "target": f"{lo:.0%}~{hi:.0%}", "ok": lo <= q <= hi,
        })
    if voice.get("n"):
        va = float(voice.get("len_avg") or 0.0)
        checks.append({
            "name": "语音条均长", "value": va,
            "target": f"<= {TARGETS['voice_len_avg_max']:.0f} 字",
            "ok": va <= TARGETS["voice_len_avg_max"],
        })
        vp = float(voice.get("len_p90") or 0.0)
        checks.append({
            "name": "语音条 p90 长度", "value": vp,
            "target": f"<= {TARGETS['voice_len_p90_max']:.0f} 字",
            "ok": vp <= TARGETS["voice_len_p90_max"],
        })
    return checks


def build_verdicts(cur: Dict[str, Any], prev: Dict[str, Any]) -> List[str]:
    """本窗 vs 上一窗 → 人话判词（周报直读结论；与 proactive_review --trend 同哲学）。

    ``cur/prev``＝{"overall": flavor_report, "voice": flavor_report,
    "suspicion_n": int, "reply": merge_reply_stats}。prev 无样本时只报绝对值。
    """
    verdicts: List[str] = []
    co, po = cur.get("overall") or {}, prev.get("overall") or {}
    cv = cur.get("voice") or {}
    if co.get("n") and po.get("n"):
        c, p = co["laugh_opener_rate"], po["laugh_opener_rate"]
        if c < p * 0.7:
            verdicts.append(f"感叹词开场收敛：笑声开场 {p:.0%} → {c:.0%}")
        elif c > p * 1.1 and c > 0.10:
            verdicts.append(f"⚠ 笑声开场未收敛（{p:.0%} → {c:.0%}）：查生成范式指令与 reply_variety 是否在跑")
        cq, pq = co["question_rate"], po["question_rate"]
        if pq > 0.45 and cq <= 0.40:
            verdicts.append(f"反问率回落：{pq:.0%} → {cq:.0%}")
    if cv.get("n"):
        if cv["len_avg"] <= TARGETS["voice_len_avg_max"]:
            verdicts.append(f"语音条长度达标（均 {cv['len_avg']:.0f} 字）")
        else:
            verdicts.append(
                f"⚠ 语音条仍偏长（均 {cv['len_avg']:.0f} 字 / p90 {cv['len_p90']:.0f}）："
                "查 B 线 split_send 与生成范式是否生效")
    cs, ps = int(cur.get("suspicion_n") or 0), int(prev.get("suspicion_n") or 0)
    if cs or ps:
        arrow = "↓" if cs < ps else ("↑⚠" if cs > ps else "持平")
        verdicts.append(f"AI 质疑入站：上窗 {ps} → 本窗 {cs}（{arrow}）")
    rep = cur.get("reply") or {}
    v, t = rep.get("voice") or {}, rep.get("text") or {}
    if (v.get("episodes") or 0) >= 5 and (t.get("episodes") or 0) >= 5:
        verdicts.append(
            f"回复率 语音 {v['reply_rate']:.0%}（{v['episodes']} 簇） vs "
            f"文字 {t['reply_rate']:.0%}（{t['episodes']} 簇）")
    return verdicts


__all__ = [
    "TARGETS", "opener_of", "flavor_report", "voice_mirror_text",
    "detect_ai_suspicion", "episode_reply_stats", "merge_reply_stats",
    "target_checks", "build_verdicts",
]
