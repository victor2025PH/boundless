"""出站口头禅账本 → 多样性提示（纯函数核心，零 IO）。

背景（2026-08-02 生产实锤）：近 14 天 1106 条出站消息里「哈哈」出现 327 次、
「咖啡」58、「抹茶」29、「手冲」20；发图配文「翻到一张之前拍的给你看」重复
9 次；几乎每条以「～/呀/啦」收尾——反应式聊天链（A 线 process_message /
B 线 generate_inbox_draft）此前没有任何跨轮防复读机制（变体守卫只在主动触达链）。

本模块只做三件事，全部可单测、零 IO：
  - ``collect_overused``：统计最近出站回复里的超限口头禅（笑词/句尾语气/
    重复开头/场景词/高频 bigram 兜底）；
  - ``build_variety_hint``：把超限项渲染成注入 prompt 的「本轮硬约束」块
    （调用方放 prompt 末端，利用 recency bias）；
  - ``parse_variety_cfg``：读 ``ai.reply_variety`` 配置（默认 enabled=False，
    本仓新子系统惯例——由主线在 overlay 灰度开）。

接线在 ``skill_manager._inject_reply_freshness``（A/B 两链同口径），消费在
``ai_client._build_context_prompt``（``_variety_hint`` 键，有键即消费）。
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Dict, List, Optional

# ── 词表 / 正则 ────────────────────────────────────────────────────────────────
# 笑词家族（哈哈/嘿嘿/呵呵/嘻嘻/hhh/haha/lol/lmao/233），按消息条数计。
_LAUGH_RE = re.compile(
    r"(?i)(哈{2,}|嘿{2,}|呵{2,}|嘻{2,}|h{3,}|(?:a?ha){2,}h?|lo+l+|lmao+|23{2,})"
)

# emoji / 变体选择符（句尾判定与开头清洗都要剥掉）
_EMOJI_RE = re.compile(
    r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2700-\u27BF"
    r"\uFE0F\u200D\u2764\u2b50]+"
)

# 句尾语气字符集（「消息以 ～/~/呀/啦/呢/哦/嘛 + 可选 emoji 结尾」；
# 哟/喔 属同族顺带收进）
_TAIL_CHARS = "～~呀啦呢哦嘛哟喔"
# 判尾前允许剥掉的收尾标点（语气字 + 「！」仍算语气尾）
_TAIL_PUNCT = "！!。.？?…‥,，、;；:： \t\r\n"

# 开头清洗：剥掉前导空白/标点/emoji（\w 在 py3 含 CJK，故 [^\w] 不伤中文）
_HEAD_STRIP_RE = re.compile(r"^[^\w]+", re.UNICODE)

_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")

# bigram 兜底的「停用字」：代词/助词/系词/常用虚字——在这些字处**切段**
# （绝不能删字后拼接，会造出假相邻 bigram）。
_STOP_CHARS = frozenset(
    "的了呢吧吗啊呀啦哦嘛哟喔嘞咯呗是我你他她它们在有和跟就都也很还挺"
    "被把等这那个到去来说想给对不没太会能可要多少什么怎么"
)


def _norm_laugh(token: str) -> str:
    """把笑词变体归一（哈哈哈哈 → 哈哈；HAHAHA → haha），供 sample 展示。"""
    t = str(token or "").lower()
    if t.startswith("哈"):
        return "哈哈"
    if t.startswith("嘿"):
        return "嘿嘿"
    if t.startswith("呵"):
        return "呵呵"
    if t.startswith("嘻"):
        return "嘻嘻"
    if t.startswith("h") and set(t) == {"h"}:
        return "hhh"
    if "ha" in t:
        return "haha"
    if t.startswith("lo"):
        return "lol"
    if t.startswith("lmao"):
        return "lmao"
    if t.startswith("23"):
        return "233"
    return t[:6]


def _tail_char(text: str) -> str:
    """消息的语气收尾字；无则空串。剥 emoji 与收尾标点后看最后一个字。"""
    s = _EMOJI_RE.sub("", str(text or "")).strip()
    s = s.rstrip(_TAIL_PUNCT)
    if s and s[-1] in _TAIL_CHARS:
        return s[-1]
    return ""


def _clean_head(text: str) -> str:
    """取消息开头（去 emoji/标点/空白后前 6 个字符）；过短返回空串。"""
    s = _EMOJI_RE.sub("", str(text or "")).strip()
    s = _HEAD_STRIP_RE.sub("", s)
    s = s.strip()
    if len(s) < 2:
        return ""
    return s[:6]


def _cjk_segments(text: str) -> List[str]:
    """提取中文连续段，并在停用字处**切段**（防删字造假相邻）。"""
    segs: List[str] = []
    for run in _CJK_RUN_RE.findall(str(text or "")):
        cur = ""
        for ch in run:
            if ch in _STOP_CHARS:
                if len(cur) >= 2:
                    segs.append(cur)
                cur = ""
            else:
                cur += ch
        if len(cur) >= 2:
            segs.append(cur)
    return segs


def _message_grams(text: str) -> set:
    """一条消息里出现的全部中文 2-4 字词（去停用字切段后的 n-gram 集合）。"""
    grams: set = set()
    for seg in _cjk_segments(text):
        n = len(seg)
        for size in (2, 3, 4):
            if n < size:
                continue
            for i in range(n - size + 1):
                grams.add(seg[i:i + size])
    return grams


def collect_overused(
    recent_outbound: List[str],
    *,
    scene_words: Optional[List[str]] = None,
    laugh_limit: int = 2,
    tail_limit: int = 3,
    head_limit: int = 2,
    scene_limit: int = 2,
    keyword_limit: int = 3,
) -> Dict[str, Any]:
    """统计最近出站回复里的超限口头禅，返回超限项（无超限返回 ``{}``）。

    全部按**消息条数**计（同一条里连打三个哈哈只算 1 条）；各 ``*_limit``
    语义 = 达到该条数即视为超限（``count >= limit``）。返回形状：

    ``{"laugh": {count, sample}, "tail": {count, sample},``
    `` "heads": [{head, count}], "scene": [{word, count}],``
    `` "keywords": [{word, count}]}``
    """
    msgs = [str(t or "").strip() for t in (recent_outbound or [])]
    msgs = [t for t in msgs if t]
    if not msgs:
        return {}
    out: Dict[str, Any] = {}

    # a) 笑词
    laugh_msgs = 0
    laugh_variants: Counter = Counter()
    for m in msgs:
        hits = _LAUGH_RE.findall(m)
        if hits:
            laugh_msgs += 1
            laugh_variants[_norm_laugh(hits[0])] += 1
    if laugh_limit > 0 and laugh_msgs >= laugh_limit:
        sample = laugh_variants.most_common(1)[0][0] if laugh_variants else "哈哈"
        out["laugh"] = {"count": laugh_msgs, "sample": sample}

    # b) 句尾语气
    tail_msgs = 0
    tail_variants: Counter = Counter()
    for m in msgs:
        ch = _tail_char(m)
        if ch:
            tail_msgs += 1
            tail_variants[ch] += 1
    if tail_limit > 0 and tail_msgs >= tail_limit:
        sample = tail_variants.most_common(1)[0][0] if tail_variants else "呀"
        out["tail"] = {"count": tail_msgs, "sample": sample}

    # c) 重复开头（前 6 字符相同的组）
    head_groups: Counter = Counter()
    for m in msgs:
        h = _clean_head(m)
        if h:
            head_groups[h] += 1
    heads = [
        {"head": h, "count": c}
        for h, c in head_groups.most_common()
        if head_limit > 0 and c >= head_limit
    ]
    if heads:
        out["heads"] = heads[:3]

    # d) 场景/口头禅词（调用方传入，如人设 tastes/selfie_scenes 里的名词）
    scene_hits: List[Dict[str, Any]] = []
    seen_scene: set = set()
    for w in (scene_words or []):
        word = str(w or "").strip()
        if not word or word.lower() in seen_scene:
            continue
        seen_scene.add(word.lower())
        cnt = sum(1 for m in msgs if word.lower() in m.lower())
        if scene_limit > 0 and cnt >= scene_limit:
            scene_hits.append({"word": word, "count": cnt})
    if scene_hits:
        scene_hits.sort(key=lambda d: -d["count"])
        out["scene"] = scene_hits[:4]

    # e) 高频 bigram 兜底（消息级去重计数；剔除笑词/已报场景词的重叠项；
    #    同计数优先保留更长的词，substring 家族只留一个代表）
    gram_counts: Counter = Counter()
    for m in msgs:
        for g in _message_grams(m):
            gram_counts[g] += 1
    scene_reported = [d["word"] for d in scene_hits]
    candidates = [
        (g, c) for g, c in gram_counts.items()
        if keyword_limit > 0 and c >= keyword_limit
        and not _LAUGH_RE.fullmatch(g)
        and not any(g in sw or sw in g for sw in scene_reported)
    ]
    candidates.sort(key=lambda t: (-t[1], -len(t[0]), t[0]))
    keywords: List[Dict[str, Any]] = []
    for g, c in candidates:
        if any(g in k["word"] or k["word"] in g for k in keywords):
            continue
        keywords.append({"word": g, "count": c})
        if len(keywords) >= 3:
            break
    if keywords:
        out["keywords"] = keywords

    return out


def build_variety_hint(
    overused: Dict[str, Any], lang: str = "zh", max_items: int = 4,
) -> str:
    """把超限项渲染成注入 prompt 的「表达多样性·本轮硬约束」块。

    空 ``overused`` 返回 ``""``。``max_items`` 限制列出的类别数（优先级：
    笑词 > 句尾 > 重复词 > 重复开头）。调用方应把该块放 prompt 末端。
    """
    if not overused or not isinstance(overused, dict):
        return ""
    zh = not str(lang or "zh").lower().startswith("en")
    facts: List[str] = []
    acts: List[str] = []

    laugh = overused.get("laugh") or {}
    if laugh.get("count"):
        n, s = laugh["count"], laugh.get("sample") or "哈哈"
        if zh:
            facts.append(f"有 {n} 条用了「{s}」这类笑法")
            acts.append("换一种笑的表达（或这轮干脆不笑）")
        else:
            facts.append(f'{n} messages used "{s}"-style laughter')
            acts.append("laugh a different way (or skip laughing this turn)")

    tail = overused.get("tail") or {}
    if tail.get("count"):
        n, s = tail["count"], tail.get("sample") or "呀"
        if zh:
            facts.append(f"有 {n} 条以「{s}」这类语气词收尾")
            acts.append("换一种收尾语气（陈述句直接收住也可以）")
        else:
            facts.append(f'{n} messages ended with the particle "{s}"')
            acts.append("end with a different tone (a plain full stop is fine)")

    words = [d.get("word", "") for d in (overused.get("scene") or [])]
    words += [d.get("word", "") for d in (overused.get("keywords") or [])]
    words = [w for w in words if w][:4]
    if words:
        listed = "、".join(f"「{w}」" for w in words) if zh else \
            ", ".join(f'"{w}"' for w in words)
        if zh:
            facts.append(f"反复提到 {listed}")
            acts.append(f"这一轮不要再提到 {listed}")
        else:
            facts.append(f"repeatedly mentioned {listed}")
            acts.append(f"do NOT mention {listed} again this turn")

    heads = overused.get("heads") or []
    if heads:
        h0 = heads[0]
        if zh:
            facts.append(f"有 {h0.get('count', 0)} 条都以「{h0.get('head', '')}」开头")
            acts.append(f"开头别再用「{h0.get('head', '')}」")
        else:
            facts.append(
                f"{h0.get('count', 0)} messages all started with "
                f'"{h0.get("head", "")}"')
            acts.append(f'do not start with "{h0.get("head", "")}" again')

    if not facts:
        return ""
    keep = max(1, int(max_items))
    facts, acts = facts[:keep], acts[:keep]
    if zh:
        return (
            "【表达多样性·本轮硬约束】你最近的回复里已经："
            + "；".join(facts)
            + "。这一轮必须：" + "；".join(acts)
            + "。违反会显得像复读机器人。"
        )
    return (
        "[Expression variety — hard constraints for THIS reply] "
        "In your recent replies you already: " + "; ".join(facts)
        + ". This turn you MUST: " + "; ".join(acts)
        + ". Violating this makes you sound like a repetitive bot."
    )


def extract_persona_words(persona: Optional[Dict[str, Any]]) -> List[str]:
    """从人设档案提取场景/口头禅候选词（tastes.likes + selfie_scenes 的中文名词）。

    只取现成字段里的中文 2-6 字段落（去停用字切段），取不到返回空列表——
    绝不为此发起新查询。上限 12 个，保序去重。
    """
    p = persona if isinstance(persona, dict) else {}
    raw: List[str] = []
    tastes = p.get("tastes") or {}
    likes = tastes.get("likes") if isinstance(tastes, dict) else None
    if isinstance(likes, (list, tuple)):
        raw.extend(str(x) for x in likes)
    elif isinstance(likes, str):
        raw.append(likes)
    scenes = p.get("selfie_scenes")
    if isinstance(scenes, (list, tuple)):
        raw.extend(str(x) for x in scenes)
    elif isinstance(scenes, str):
        raw.append(scenes)
    words: List[str] = []
    seen: set = set()
    for item in raw:
        for seg in _cjk_segments(item):
            w = seg[:6]
            if w and w not in seen:
                seen.add(w)
                words.append(w)
            if len(words) >= 12:
                return words
    return words


def parse_variety_cfg(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """读 ``ai.reply_variety``，返回归一化配置（全有默认值，永不抛）。

    默认 ``enabled: False``（本仓新子系统惯例，主线在 overlay 灰度开）。
    """
    cfg = config if isinstance(config, dict) else {}
    ai_cfg = cfg.get("ai") if isinstance(cfg.get("ai"), dict) else {}
    rv = ai_cfg.get("reply_variety") if isinstance(ai_cfg, dict) else None
    if not isinstance(rv, dict):
        rv = {}

    def _i(key: str, default: int, lo: int = 0, hi: int = 100) -> int:
        try:
            v = int(rv.get(key, default))
        except (TypeError, ValueError):
            v = int(default)
        return max(lo, min(hi, v))

    return {
        "enabled": bool(rv.get("enabled", False)),
        "window": _i("window", 12, lo=1, hi=50),
        "max_items": _i("max_items", 4, lo=1, hi=8),
        "laugh_limit": _i("laugh_limit", 2, lo=0, hi=50),
        "tail_limit": _i("tail_limit", 3, lo=0, hi=50),
        "head_limit": _i("head_limit", 2, lo=0, hi=50),
        "scene_limit": _i("scene_limit", 2, lo=0, hi=50),
        "keyword_limit": _i("keyword_limit", 3, lo=0, hi=50),
    }


__all__ = [
    "collect_overused",
    "build_variety_hint",
    "extract_persona_words",
    "parse_variety_cfg",
]
