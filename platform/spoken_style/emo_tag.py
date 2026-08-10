# -*- coding: utf-8 -*-
"""LLM 情绪标记协议（独立模块，2026-08-01）。

协议：回复开头自报本轮主情绪 → 驱动 TTS 情感通道；用户永远不该看见字样。
  [情绪:开心|中|有点得意]  = 标签|强度(弱/中/强)|神态(可选)

本模块是单一真相（hub / colloquial_rewrite / conv_patrol / metrics 同源引用）：
  · 捕获/剥离（结构容忍变体包装：[${情绪:…}] / （情绪:…） 等）
  · 形态分类（canonical 原形 / variant 漂移）——捕获时记账，不依赖巡检看到原文
  · 出口 canary——剥净后仍检出「情绪+冒号+括号族」共现 → 泄漏事件
  · 段首/段尾摘接（口语化改写层用，防改写器丢掉/发明标记）

事件通过可选 hook 上报（metrics.record_emo_tag_event）；模块本身不依赖 metrics，
工具/单测可独立 import。CONV_EMO_TAGS=0 时 hub 不注入提示词，但剥离/canary 仍生效
（兜底历史污染与模型漂移）。
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Callable, Optional

logger = logging.getLogger("emo_tag")

ENABLED = os.environ.get("CONV_EMO_TAGS", "1") != "0"
EMO_TEXT_ON = os.environ.get("CONV_EMO_TEXT", "1") != "0"

# 结构容忍：开闭括号放宽到 [ 【 ( （ { ｛ 家族，并容忍 $ ＄ + 花括号包装层。
# 误伤面：须同时命中「括号+情绪+冒号+≤8字标签+闭括」才会被剥。
TAG_RE = re.compile(
    r"[\[【（(｛{]{1,3}\s*[$＄]?\s*[｛{]?\s*情绪\s*[:：]\s*([^|｜\]】）)｝}\n]{1,8})"
    r"(?:\s*[|｜]\s*([^|｜\]】）)｝}\n]{1,8}))?"
    r"(?:\s*[|｜]\s*([^\]】）)｝}\n]{1,30}))?"
    r"\s*[\]】）)｝}]{1,3}")
FRAG_RE = re.compile(
    r"[\[【｛{]{1,3}\s*[$＄]?\s*[｛{]?\s*情绪[^\]】｝}\n]{0,44}$")
# 严格原形（形态分类用）：整段匹配才算 canonical
_CANONICAL_RE = re.compile(
    r"^[\[【]\s*情绪\s*[:：]\s*[^|｜\]】\n]{1,8}"
    r"(?:\s*[|｜]\s*[^|｜\]】\n]{1,8})?"
    r"(?:\s*[|｜]\s*[^\]】\n]{1,30})?\s*[\]】]$")
# 出口 canary：剥净后仍出现「情绪+冒号」且邻近括号/$ 族 → 泄漏残留
_LEAK_HINT_RE = re.compile(
    r"(?:[\[【（(｛{$＄].{0,8})?情绪\s*[:：]"
    r"|情绪\s*[:：][^。！？\n]{0,24}[\]】）)｝}]")

# 口语化改写层：段首/段尾控制标签摘接（含带神态的长标记，内长 44）
LEAD_TAG_RE = re.compile(r"^\s*(\[[^\[\]\n]{1,44}\]|【[^【】\n]{1,40}】)\s*")
TAIL_TAG_RE = re.compile(r"\s*(\[[^\[\]\n]{1,44}\]|【[^【】\n]{1,40}】)\s*$")

LABELS = {
    "开心": "happy", "高兴": "happy", "喜悦": "happy", "俏皮": "happy",
    "激动": "excited", "兴奋": "excited",
    "难过": "sad", "伤心": "sad", "委屈": "sad", "低落": "sad",
    "温柔": "gentle", "安慰": "gentle", "心疼": "gentle",
    "认真": "serious", "严肃": "serious",
    "平静": "calm", "淡定": "calm",
    "惊讶": "surprised", "吃惊": "surprised",
    "生气": "angry", "愤怒": "angry",
    "害怕": "fearful", "紧张": "fearful",
    "嫌弃": "disgusted",
    "中性": "neutral", "自然": "neutral",
}
INTENSITY = {"弱": 0.7, "中": 1.0, "强": 1.3}
TTL_S = 180.0

PROMPT = (
    "【情绪标记】每条回复的最开头，先用一个标记声明这条回复的主情绪，格式固定："
    "[情绪:标签|强度] 或 [情绪:标签|强度|神态]。标签只能从这里选："
    "开心/激动/难过/温柔/认真/平静/惊讶/生气/中性；强度只能是 弱/中/强；"
    "神态可选，是一小句你此刻说话状态的口语描述（不超过12个字、里面不用标点，"
    "比如「忍着笑」「有点委屈」「压低声音安慰」）。例：[情绪:开心|中|有点得意]。"
    "寒暄和普通答疑写 [情绪:中性|弱] 就行。"
    "标记必须按原形书写：不要外加 $ 或花括号（不要写成 [${情绪:…}]），不要加引号，也不要改用圆括号。"
    "这个标记只驱动配音语气，不会显示给用户；只在开头出现一次，正文里不要再写任何方括号标记。"
)

# 契约巡检用的短探针话术（覆盖寒暄/情绪触发两类）
PROBE_PROMPTS = (
    "你好啊。",
    "今天过得怎么样？",
    "刚听到一个超好笑的事，跟你分享一下！",
    "我有点难过，陪我说说话吧。",
    "这件事挺重要的，你认真听一下。",
)

_turn_tags: dict = {}   # session_id -> {label, raw, mult, desc, form, ts}
_event_hook: Optional[Callable] = None
# canary 同文案节流：同一样本 60s 内只记一次，防流式多出口重复刷库
_canary_seen: dict = {}
_CANARY_DEDUP_S = 60.0


def set_event_hook(fn: Optional[Callable]) -> None:
    """metrics 在 import 后挂上：hook(kind, *, profile, session_id, sample, form)。"""
    global _event_hook
    _event_hook = fn


def _emit(kind: str, *, profile: str = "", session_id: str = "",
          sample: str = "", form: str = "") -> None:
    if _event_hook is None:
        return
    try:
        _event_hook(kind, profile=profile or "", session_id=session_id or "",
                    sample=(sample or "")[:120], form=form or "")
    except Exception as e:
        logger.debug(f"emo_tag event hook 失败: {e}")


def classify_span(span: str) -> str:
    """命中片段形态：canonical（协议原形）/ variant（漂移包装）。"""
    s = (span or "").strip()
    if not s:
        return "none"
    return "canonical" if _CANONICAL_RE.match(s) else "variant"


def classify_text(text: str) -> dict:
    """巡检/探针用：在一段 LLM 原文里找情绪标记并分类。
    返回 {form, span, label, intensity, desc, position}；无标记 form=none。"""
    if not text or "情绪" not in text:
        return {"form": "none", "span": "", "label": "", "intensity": "",
                "desc": "", "position": -1}
    m = TAG_RE.search(text)
    if not m:
        if FRAG_RE.search(text):
            return {"form": "frag", "span": text[-48:], "label": "",
                    "intensity": "", "desc": "", "position": max(0, len(text) - 48)}
        return {"form": "none", "span": "", "label": "", "intensity": "",
                "desc": "", "position": -1}
    span = m.group(0)
    raw = (m.group(1) or "").strip()
    return {
        "form": classify_span(span),
        "span": span,
        "label": LABELS.get(raw, ""),
        "raw": raw,
        "intensity": (m.group(2) or "").strip(),
        "desc": (m.group(3) or "").strip(),
        "position": m.start(),
    }


def strip_tags(text: str) -> str:
    """只剥情绪标记（整词+残片），不动副语言/其他方括号。"""
    if not text or "情绪" not in text:
        return text
    t = TAG_RE.sub("", text)
    t = FRAG_RE.sub("", t)
    if t != text:
        t = re.sub(r"^[，、。！？!?…~～\s]+", "", t)
        t = re.sub(r"[ \t\u3000]{2,}", " ", t)
        t = re.sub(r"[ \t\u3000]+(?=[。，、！？；：．,.!?;:…])", "", t)
    return t


def canary_scan(text: str) -> Optional[str]:
    """剥净后的用户可见文本：仍像泄漏则返回命中片段，否则 None。"""
    if not text or "情绪" not in text:
        return None
    m = _LEAK_HINT_RE.search(text)
    return m.group(0) if m else None


def peel_lead_tail(text: str) -> tuple:
    """口语化改写层：摘段首/段尾控制标签。回 (lead, body, tail)。"""
    t = (text or "").strip()
    lead = tail = ""
    m = LEAD_TAG_RE.match(t)
    if m:
        lead, t = m.group(1), t[m.end():].strip()
    mt = TAIL_TAG_RE.search(t)
    if mt:
        tail, t = mt.group(1), t[:mt.start()].rstrip()
    return lead, t, tail


def capture(text: str, session_id: str, *, profile: str = "") -> str:
    """解析并剥离标记；命中则写会话态 + 上报 hit_canonical/hit_variant。
    返回剥净文本；无标记原样返回。"""
    if not text or "情绪" not in text:
        return text
    m = TAG_RE.search(text)
    if m:
        raw = (m.group(1) or "").strip()
        lab = LABELS.get(raw)
        form = classify_span(m.group(0))
        sid = session_id or "default"
        prev = _turn_tags.get(sid)
        # sentence 出口与 tts_fn 会对同一轮文本各捕获一次——只在「本轮首次落库」记账，
        # 避免 hit 事件双计（看板变体率被稀释/虚高）。
        first_hit = not (prev and prev.get("label"))
        if lab:
            _turn_tags[sid] = {
                "label": lab, "raw": raw,
                "mult": INTENSITY.get((m.group(2) or "中").strip(), 1.0),
                "desc": (m.group(3) or "").strip(),
                "form": form,
                "ts": time.time(),
            }
            logger.info(f"[EmoTag] {raw}|{(m.group(2) or '中').strip()}"
                        f"{('|' + m.group(3).strip()) if m.group(3) else ''} "
                        f"→ {lab} ({form})")
            if first_hit:
                _emit(f"hit_{form}", profile=profile, session_id=session_id,
                      sample=m.group(0), form=form)
        elif first_hit:
            # 标签字不在白名单：仍剥掉字样，记 unknown（不算命中也不算泄漏）
            _emit("hit_unknown", profile=profile, session_id=session_id,
                  sample=m.group(0), form=form)
    t = TAG_RE.sub("", text)
    t = FRAG_RE.sub("", t)
    if t != text:
        t = re.sub(r"^[，、。！？!?…~～\s]+", "", t)
    return t


def get(session_id: str) -> dict:
    d = _turn_tags.get(session_id or "default")
    if d and time.time() - float(d.get("ts", 0)) <= TTL_S:
        return d
    return {}


def clear(session_id: str) -> None:
    _turn_tags.pop(session_id or "default", None)


def note_miss(session_id: str, *, profile: str = "") -> None:
    """轮末：协议开启但本轮未捕获到任何标记 → miss（模型忘了报）。"""
    if not ENABLED:
        return
    if get(session_id):
        return
    _emit("miss", profile=profile, session_id=session_id, sample="", form="none")


def canary_check(text: str, *, profile: str = "", session_id: str = "",
                 sink: str = "") -> bool:
    """出口 canary：用户可见文本剥净后仍像泄漏 → 记 leak 事件。
    返回 True=检出泄漏。同文案 60s 去重，防 sentence+reply+tts 多出口刷库。"""
    hit = canary_scan(text)
    if not hit:
        return False
    key = f"{(session_id or '')}|{hit}"
    now = time.time()
    last = _canary_seen.get(key, 0.0)
    if now - last < _CANARY_DEDUP_S:
        return True
    _canary_seen[key] = now
    # 清过期去重表（有界）
    if len(_canary_seen) > 200:
        cutoff = now - _CANARY_DEDUP_S
        for k in [k for k, t in _canary_seen.items() if t < cutoff]:
            _canary_seen.pop(k, None)
    sample = f"[{sink}] {text[:80]}" if sink else text[:80]
    logger.warning(f"[EmoTag/canary] 出口泄漏 sink={sink or '-'} "
                   f"hit={hit!r} sample={text[:60]!r}")
    _emit("leak", profile=profile, session_id=session_id,
          sample=sample, form="leak")
    return True


def score_probe_batch(replies: list) -> dict:
    """契约巡检汇总：对一批 LLM 原文（未剥）统计原形/变体/漏报/残片。
    replies: [{text, ...}] 或纯 str 列表。"""
    n = len(replies or [])
    counts = {"canonical": 0, "variant": 0, "none": 0, "frag": 0, "unknown": 0}
    samples = []
    for i, r in enumerate(replies or []):
        text = r if isinstance(r, str) else (r.get("text") or r.get("reply") or "")
        info = classify_text(text)
        form = info["form"]
        if form == "canonical" and not info.get("label"):
            form = "unknown"
            counts["unknown"] += 1
        else:
            counts[form] = counts.get(form, 0) + 1
        if form != "none" or i < 3:
            samples.append({"i": i, "form": form,
                            "span": (info.get("span") or "")[:40],
                            "position": info.get("position", -1),
                            "preview": (text or "")[:60]})
    tagged = counts["canonical"] + counts["variant"] + counts["unknown"]
    return {
        "n": n,
        "counts": counts,
        "canonical_rate": round(counts["canonical"] / n, 3) if n else 0.0,
        "variant_rate": round(counts["variant"] / tagged, 3) if tagged else 0.0,
        "miss_rate": round(counts["none"] / n, 3) if n else 0.0,
        "samples": samples[:12],
        "ok": (n > 0 and counts["none"] / n <= 0.4
               and (tagged == 0 or counts["variant"] / tagged <= 0.3)
               and counts["frag"] == 0),
    }
