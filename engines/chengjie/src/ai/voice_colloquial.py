"""口语化改写层 — 把「给眼睛看的书面文本」改写成「给耳朵听的口语」（活人感 P0）。

为什么需要（2026-07-14 语音活人感诊断）：TTS「一股 AI 味 / 像豆包」的根因之一是
**待合成文本本身是书面语**——LLM 生成的回复为「阅读」优化（完整句、书面连接词、
无口语碎句/语气词/迟疑），再好的 TTS 念出来也像播报。真人说话是碎的：有「嗯…」
「其实吧」的迟疑开场、有「不过」而非「然而」、句末带软化语气。本模块在**送 TTS 引擎
合成之前**把文本做轻度口语化改写，**只作用于「念出来的话」，不动镜像/记忆用的原文**
（分「给眼睛看的字」和「给耳朵听的口语」两条）。

设计原则（与 voice_emotion.inject_paralinguistic 同族）：
  - **纯函数、无 IO/网络**：可单测、零延迟、零 GPU、零 LLM 成本。
  - **确定性**：用 crc32(原文) 定「加不加/加哪个」——同文本同结果，TTS 缓存/预渲染
    键因此稳定（非确定性改写会击穿缓存 + 破坏文本一致性）。
  - **情绪门控**：serious/apologetic（庄重场合）不加软化词/随意替换；neutral/warm/
    playful 才放开。**neutral 也改写**——大量日常对话情绪走 neutral 保真路径（音色最像
    但韵律最平），正需要文字层口语化补活人感。
  - **短句 no-op**：≤min_chars 的短句直接原样返回——短句本就口语，且预渲染库存的是
    短句（问候语等），no-op 保住预渲染命中率零损耗。
  - **宁少勿多**：过度口语化 = 做作/油腻/破坏人设。每条至多 max_inserts 个「加词」，
    词替换只收语义无损的高置信映射。
  - **防御式**：任何脏输入/异常都退化成原文，绝不抛给 TTS 主流程。

可单测纯函数：colloquialize / _lexical_swap / _lead_filler / normalize_colloquial_emotion。
"""
from __future__ import annotations

import re
import zlib
from typing import Any, Dict, List, Optional, Tuple

# 情绪档（对齐 voice_emotion.EMOTIONS 的子集语义；未知 → neutral）。
_CASUAL_OK = ("neutral", "warm", "happy", "playful", "excited", "calm")
_FORMAL = ("serious", "apologetic")          # 庄重：只做中性词替换，不加软化/随意口语
_SOFT_LEAD_SKIP = ("sad", "empathetic")      # 句首叹气让位给副语言标记 [sigh]，不加迟疑词

# ── 书面词 → 口语词替换 ──────────────────────────────────────────────────────
# SAFE：语义/正式度无损，任何情绪都可（连接词的口语等价，念出来更自然）。
_LEXICAL_SAFE: Tuple[Tuple[str, str], ...] = (
    ("因此", "所以"),
    ("然而", "不过"),
    ("但是", "不过"),
    ("目前", "现在"),
    ("立即", "马上"),
    ("立刻", "马上"),
    ("倘若", "要是"),
    ("务必", "一定"),
    ("以及", "还有"),
    ("并且", "而且"),
)
# CASUAL：更随意，仅非正式情绪用（serious/apologetic 保持书面）。
_LEXICAL_CASUAL: Tuple[Tuple[str, str], ...] = (
    ("是否", "是不是"),
    ("非常", "特别"),
    ("十分", "特别"),
    ("如果", "要是"),
    ("可是", "不过"),
    ("竟然", "居然"),
)

# ── 句首软化 / 迟疑词（真人开口的自然连接；情绪分档）────────────────────────
# 每档 2-3 个变体，crc32 确定性轮换。刻意克制：只在长句、低频、每条至多 1 个。
# 选词偏「万能续接/迟疑」（其实/话说/说起来 几乎任何语境都自然），刻意少用
# 「对了」这类带「想起新话题」语义的词（接续性陈述句时会突兀）——规则层无法判断
# 语境，选词保守是降低「语义不搭」的关键（真正的语境贴合留给下一阶段 LLM 口语化）。
_LEAD_FILLERS: Dict[str, Tuple[str, ...]] = {
    "neutral": ("其实", "话说", "说起来"),
    "warm":    ("其实", "话说", "诶"),
    "calm":    ("其实", "说起来"),
    "happy":   ("诶", "话说", "对了"),   # happy＝分享场景，「对了！跟你说」自然
    "playful": ("诶嘿", "哎呀", "话说"),
    "excited": ("哇", "诶"),
}
# 句首已是这些词/符号 → 不加迟疑词（防「其实，其实吧」叠加、防破坏问候开场）。
_LEAD_SKIP_RE = re.compile(
    r"^\s*[「『\"'（(【\[]?\s*"
    r"(嗯+|诶+|哦+|唉+|呃+|啊+|哎+|嘿+|哇+|唔+|其实|话说|对了|不过|所以|然后|"
    r"那么?|这个|就是|说起来|你好|您好|亲爱|宝贝|哈喽|hi|hello)",
    re.IGNORECASE,
)

# ── 句末语气助词（最易做作，默认关；仅暖/俏皮/开心情绪的陈述句软化）──────────
_FINAL_PARTICLES: Dict[str, Tuple[str, ...]] = {
    "warm":    ("呀", "呢"),
    "happy":   ("呀", "啦"),
    "playful": ("呀", "啦", "哦"),
}
# 句末已是语气助词/疑问/感叹 → 不加（避免「好啦呀」「好吗呀」）。
_FINAL_SKIP_CHARS = set("呀呢啦哦吗吧嘛么呗哈~")


# ── 语言门控（关键护栏）────────────────────────────────────────────────────
# 本模块的口语词/迟疑词是**真中文字符会被念出来**（不同于副语言标记 [sigh] 在
# tokenizer 层消费不读出）。给英文/日文句子前加中文「其实，」= garble 事故
# （与「中文声纹念外语」同族）。故只对**中文为主**的文本改写：含平假名/片假名/
# 谚文 → 判为日/韩文跳过；CJK 汉字占比不足 → 判为拉丁语系跳过。
_KANA_HANGUL_RE = re.compile(r"[\u3040-\u30ff\uac00-\ud7af]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
# 从 quirks 引号段提取口头禅（中英文引号均支持）
_QUOTED_RE = re.compile(r'[「『""'']([^「」『』""'']{1,16})[」』""'']')
_LEAD_PHRASE_MAX = 6   # 句首词过长会像念标题，刻意克制


def _is_chinese_dominant(text: str, *, min_ratio: float = 0.3) -> bool:
    """文本是否中文为主（可安全做中文口语化）。含假名/谚文即判非中文。"""
    if not text:
        return False
    if _KANA_HANGUL_RE.search(text):        # 日文假名 / 韩文谚文 → 非中文
        return False
    cjk = len(_CJK_RE.findall(text))
    if cjk == 0:
        return False
    # 以「字母/汉字」为分母算汉字占比（标点/数字/空格不计），避免长串标点稀释
    letters = sum(1 for ch in text if ch.isalpha() or _CJK_RE.match(ch))
    if letters <= 0:
        return False
    return (cjk / letters) >= min_ratio


def normalize_colloquial_emotion(spec: Any) -> Tuple[str, float]:
    """从 EmotionSpec/字符串/None 提取 (emotion, intensity)。防御式 duck-typing。"""
    if spec is None:
        return "neutral", 0.6
    if isinstance(spec, str):
        return (spec.strip().lower() or "neutral"), 0.6
    emo = str(getattr(spec, "emotion", "") or "neutral").strip().lower()
    try:
        inten = float(getattr(spec, "intensity", 0.6))
    except (TypeError, ValueError):
        inten = 0.6
    return (emo or "neutral"), max(0.0, min(1.0, inten))


def _lexical_swap(text: str, *, casual: bool) -> str:
    """书面词 → 口语词等义替换（不增词、不占 insert 额度）。全量替换=确定性。"""
    out = text
    for a, b in _LEXICAL_SAFE:
        if a in out:
            out = out.replace(a, b)
    if casual:
        for a, b in _LEXICAL_CASUAL:
            if a in out:
                out = out.replace(a, b)
    return out


def _normalize_lead_phrase(raw: str) -> str:
    """去掉引号/句末标点，得到可念的中文句首词。"""
    s = str(raw or "").strip().strip("「」『』\"'")
    return re.sub(r"[！!。，,、…~？?]", "", s).strip()


def _valid_lead_phrase(phrase: str) -> bool:
    """口头禅是否适合作句首连接词（短、中文、非完整从句）。"""
    if not phrase or len(phrase) > _LEAD_PHRASE_MAX:
        return False
    if not _is_chinese_dominant(phrase, min_ratio=0.85):
        return False
    # 过长且带谓语结构 → 像半句话而非口头禅
    if len(phrase) >= 5 and any(c in phrase for c in ("的是", "在了", "有没有")):
        return False
    return True


def parse_persona_lead_phrases(
    quirks: str = "",
    *,
    catchphrase: str = "",
) -> Tuple[str, ...]:
    """从人设 quirks / voice_profile.catchphrase 提取句首口头禅（1–6 字中文）。

    quirks 示例：``喜欢说"哇！""啊对对对"`` → (``哇``, ``啊对对对``)。
    显式 catchphrase（逗号分隔）优先；引号段去重；至多返回 6 个变体。
    """
    seen: set = set()
    out: List[str] = []

    def _add(raw: str) -> None:
        ph = _normalize_lead_phrase(raw)
        if not _valid_lead_phrase(ph) or ph in seen:
            return
        seen.add(ph)
        out.append(ph)

    for part in re.split(r"[,，、/|]+", str(catchphrase or "")):
        if part.strip():
            _add(part.strip())
    for m in _QUOTED_RE.finditer(str(quirks or "")):
        _add(m.group(1))
    return tuple(out[:6])


def build_voice_style_hint(
    instruct_style: str = "",
    quirks: str = "",
    *,
    catchphrase: str = "",
) -> str:
    """拼 LLM 口语化用的语气/口头禅提示（instruct_style + quirks 合一）。"""
    parts: List[str] = []
    style = str(instruct_style or "").strip()
    if style:
        parts.append(f"声线底色：{style}")
    leads = parse_persona_lead_phrases(quirks, catchphrase=catchphrase)
    if leads:
        parts.append(f"标志性口头禅（可自然用于句首）：{'、'.join(leads)}")
    elif str(quirks or "").strip():
        q = str(quirks).strip().replace("\n", " ")
        parts.append(f"说话习惯：{q[:80]}")
    return "；".join(parts)


def _lead_filler(
    text: str,
    emotion: str,
    seed: int,
    *,
    prob: float,
    persona_leads: Tuple[str, ...] = (),
) -> Tuple[str, bool]:
    """句首软化/迟疑词注入。返回 (新文本, 是否注入)。情绪+概率+句首形态三重门控。"""
    variants = persona_leads if persona_leads else _LEAD_FILLERS.get(emotion)
    if not variants:
        return text, False
    if _LEAD_SKIP_RE.match(text):          # 句首已是语气词/问候 → 不叠加
        return text, False
    if ((seed >> 5) % 100) >= int(min(0.95, prob) * 100):
        return text, False
    lead = variants[(seed >> 13) % len(variants)]
    return f"{lead}，{text}", True


def _sentence_final(text: str, emotion: str, seed: int, *, prob: float) -> Tuple[str, bool]:
    """句末语气助词软化（默认关）。仅暖/俏皮/开心的陈述句，避免疑问/感叹/已有助词。"""
    particles = _FINAL_PARTICLES.get(emotion)
    if not particles:
        return text, False
    if ((seed >> 9) % 100) >= int(min(0.95, prob) * 100):
        return text, False
    stripped = text.rstrip()
    # 去掉可能的句末标点看真正的末字
    tail = stripped.rstrip("。.！!？?，,、…~ ")
    if not tail:
        return text, False
    last = tail[-1]
    if last in _FINAL_SKIP_CHARS:          # 已带语气助词 → 不叠
        return text, False
    # 疑问/感叹句（末尾标点是 ？！）不软化——语气助词会削弱语气
    end_punc = stripped[len(tail):]
    if any(p in end_punc for p in ("？", "?", "！", "!")):
        return text, False
    particle = particles[(seed >> 17) % len(particles)]
    # 在句末标点前插入助词：「好的。」→「好的呀。」；无标点则直接补
    if end_punc:
        return tail + particle + end_punc, True
    return tail + particle, True


def colloquialize(
    text: str,
    spec: Any = None,
    *,
    min_chars: int = 12,
    max_inserts: int = 2,
    enable_fillers: bool = True,
    enable_sentence_final: bool = False,
    enable_lexical: bool = True,
    lead_prob: float = 0.55,
    final_prob: float = 0.4,
    persona_leads: Tuple[str, ...] = (),
) -> str:
    """把书面 TTS 文本轻度口语化 → 减「念稿感」。纯函数、确定性、防御式。

    仅作用于**送引擎合成的文本**，调用方须保住原文用于镜像/记忆（分眼睛/耳朵两版）。

    - ``min_chars``：短句 no-op 阈值（≤ 此长度原样返回，保预渲染命中 + 短句本就口语）。
    - ``max_inserts``：「加词」总量上限（句首迟疑 + 句末助词），词替换不占额度。
    - ``enable_*``：三类手段分别可关（lexical 词替换 / fillers 句首迟疑 / sentence_final 句末助词）。
    - 情绪门控：serious/apologetic 只做 SAFE 词替换；sad/empathetic 句首让位副语言标记。
    - neutral 也改写（日常对话主路，最需要文字层活人感）。

    不适用（空/短/异常）→ 原文不变。
    """
    try:
        t = str(text or "")
        core = t.strip()
        if len(core) < max(1, int(min_chars)):
            return t                        # 短句 no-op（保预渲染 + 短句本就口语）
        if not _is_chinese_dominant(core):
            return t                        # 非中文文本 → 跳过（中文口语词会 garble 外语）
        emotion, _inten = normalize_colloquial_emotion(spec)
        formal = emotion in _FORMAL
        seed = zlib.crc32(core.encode("utf-8"))

        out = t
        # 1) 词替换（等义，不占额度）：庄重情绪只做 SAFE 组
        if enable_lexical:
            out = _lexical_swap(out, casual=not formal)

        inserts = 0
        cap = max(0, int(max_inserts))
        # 2) 句首迟疑/软化词（占 1 额度）：庄重情绪 & sad/empathetic 跳过
        if (enable_fillers and inserts < cap and not formal
                and emotion not in _SOFT_LEAD_SKIP):
            out, hit = _lead_filler(
                out, emotion, seed, prob=lead_prob, persona_leads=persona_leads)
            if hit:
                inserts += 1
        # 3) 句末语气助词（占 1 额度，默认关）：仅暖/俏皮/开心
        if enable_sentence_final and inserts < cap and not formal:
            out, hit = _sentence_final(out, emotion, seed, prob=final_prob)
            if hit:
                inserts += 1
        return out
    except Exception:
        return str(text or "")


__all__ = ["colloquialize", "normalize_colloquial_emotion"]
