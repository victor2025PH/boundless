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
    ("所以说", "所以"),
    ("然而", "不过"),
    ("但是", "不过"),
    ("目前", "现在"),
    ("立即", "马上"),
    ("立刻", "马上"),
    ("倘若", "要是"),
    ("务必", "一定"),
    ("以及", "还有"),
    ("并且", "而且"),
    ("无需", "不用"),
    ("不必", "不用"),
)
# CASUAL：更随意，仅非正式情绪用（serious/apologetic 保持书面）。
_LEXICAL_CASUAL: Tuple[Tuple[str, str], ...] = (
    ("是否", "是不是"),
    ("非常", "特别"),
    ("十分", "特别"),
    ("如果", "要是"),
    ("可是", "不过"),
    ("竟然", "居然"),
    ("进行", "做"),
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
# 早安/晚安类问候开场同列（P0 2026-08-05 min_chars 降到 6 后短问候进改写范围，
# 「话说，早安」这种句首注入比书面腔更假）。
_LEAD_SKIP_RE = re.compile(
    r"^\s*[「『\"'（(【\[]?\s*"
    r"(嗯+|诶+|哦+|唉+|呃+|啊+|哎+|嘿+|哇+|唔+|其实|话说|对了|不过|所以|然后|"
    r"那么?|这个|就是|说起来|你好|您好|亲爱|宝贝|哈喽|早安|晚安|午安|早上好|"
    r"晚上好|早呀|早哇|hi|hello)",
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

# ── 真人微特征（C+，2026-07-24）：思考重复 / 轻笑声 ──────────────────────────
# IndexTTS/Cosy 念「哈哈/哈哈哈」极假（播音腔连环哈）——轻笑只用单音节「嘿」，
# 开心主要靠 emotion=happy，不靠念笑字。思考重复＝「我觉得……我觉得」。
_SOFT_LAUGHS: Dict[str, Tuple[str, ...]] = {
    "happy":   ("嘿",),
    "playful": ("嘿",),
    "excited": ("嘿",),
    # warm 不加笑——温柔态硬插笑更假
}
_LAUGH_SKIP_RE = re.compile(r"^\s*[「『\"']?\s*(哈{2,}|嘿+|呵{2,}|嘻{2,})")
# 不宜作思考重复锚点的虚词/连接（重复会像故障而非思考）
_THINK_SKIP = frozenset({
    "其实", "话说", "然后", "所以", "不过", "因为", "如果", "要是", "这个",
    "那个", "就是", "可以", "已经", "还是", "或者", "虽然", "但是", "而且",
    "还有", "我们", "你们", "他们", "什么", "怎么", "一下", "一点", "一些",
})
_THINK_WORD_RE = re.compile(r"[\u4e00-\u9fff]{2}")


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


def _is_english_dominant(text: str) -> bool:
    """拉丁字母为主、可走英文口语化（Claire 等）。假名/谚文直接否。"""
    if not text or _KANA_HANGUL_RE.search(text):
        return False
    latin = sum(1 for ch in text if ("A" <= ch <= "Z") or ("a" <= ch <= "z"))
    cjk = len(_CJK_RE.findall(text))
    return latin >= 8 and latin > cjk * 2


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
        # 陪伴口语：敬语「您」→「你」（保留「您好」开场）
        if "您" in out:
            out = re.sub(r"您(?!好)", "你", out)
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


# ── 地域方言词汇档（P1 2026-08-05，文本层方言 MVP）──────────────────────────
# 只做「词汇/语气词」级的地域口味，绝不做拼音化方言书写（阔以/灰常）——TTS 按
# 普通话音系发音，生僻方言写法反而 garble；「打字普通话、语音带乡音词」也正是
# 真人的常态（比文本气泡同步方言化更自然）。注入走 LLM 口语化的 style 通道
# （build_voice_style_hint → llm_colloquialize(style=)）：LLM 内存/落盘缓存键都
# 含 style → 人设开关/换档自动失效旧缓存，零额外接线。规则档刻意不做方言
# （无语境的词替换在方言上误伤率高）；LLM 三级端点全挂时回落标准普通话＝软降级。
# 词表准入标准：普通话读者能懂 + 普通话音系可自然发音 + 无歧义贬义。
# 粤语刻意不进本表——「系咯/唔使」是粤语书写形，普通话 TTS 念出来是 garble，
# 粤语要走声学层（zh-HK 路由/粤语参考音），见 voice_lang_route.cantonese。
# ⚠ 也别试图把粤语加进来（2026-08-30 论证过的顺序死结）：本表经 colloquial 改写
# 发生在 TTSPipeline.synthesize **内部**，而音色路由（voice_lang_route）在发送层
# **之前**已按原文定了音色——改写出的粤文会被已定的普通话音色硬念。粤语人设的
# 正确形态＝persona_manager._format_persona_instructions 的「粤语人设」prompt 块
# （voice_profile.dialect_flavor=cantonese → 书面稿原生粤文 → 路由按粤文命中切
# 粤语音色/克隆），文字与语音天然一致。
# ⚠ 2026-08-30 出货闸门：本表**不再注入**（见 dialect_style_line → is_shipped_dialect）。
# 没有对应语言模型时，「普通话夹乡音词」会假装成闽南语/川渝。词表留档备查，
# 粤语正确形态仍是 persona prompt 粤文 + lang_voice_route，不进本表。
_DIALECT_PACKS: Dict[str, Dict[str, str]] = {
    "chuanyu": {
        "label": "川渝",
        "words": "巴适、要得、撒子、咋个、莫得、安逸、对头、要不得",
    },
    "dongbei": {
        "label": "东北",
        "words": "唠嗑、贼（贼好/贼香）、老…了（老好了）、咋整、麻溜、得劲",
    },
    "taiwan": {
        "label": "台湾腔",
        "words": "蛮…的、超…的（超好吃）、诶、好啦好啦、真的假的、还不错诶",
    },
    # 2026-08-19 扩容（老板拍板方言首发：湖南=陈默、闽南=苏婉；北京备用）。
    # 准入同上：普通话读者能懂 + 普通话音系可自然发音——「湘普/闽普」效果本身
    # 就是真人打字聊天的常态，不做拼音化方言书写。
    # 2026-08-19 晚二次升级（老板听评「陈默不像湖南/苏婉还是普通话/林小雨像东北」）：
    # 词汇层的天花板=「带地方词的普通话」，真口音必须换带口音的参考音（声学层）。
    # 在天花板内再榨一点：① 词 → **句式**（「我有+动词」「足/有够+形容词」这类语法
    # 标记比孤词辨识度高一个量级）；② 南方档显式反北方腔（贼/咋/儿化正是「台湾词
    # 念出东北味」的文本层帮凶）。patterns/avoid 为可选键，dialect_style_line 消费。
    "hunan": {
        "label": "湖南",
        "words": "搞么子、恰饭（恰=吃）、蛮（蛮好/蛮扎实）、要得咯、莫急、"
                 "何解（为什么）、晓得（知道）、咯/啵（句尾）",
        "patterns": "「要得要得」叠用应承、句尾用「咯/啵」轻轻收（好啵/走咯）、"
                    "反问用「何解咯」",
        "avoid": "北方腔口头语（贼、咋整、儿化词）",
    },
    "minnan": {
        "label": "闽南",
        "words": "哎哟、拍谢（不好意思）、厉害喔、讲（代替「说」）、"
                 "足（足好吃/足麻烦）、啦/哦/内（句尾）、金拚（很拼）",
        "patterns": "「我有+动词」句式（我有去过/我有跟他讲）、「足+形容词」、"
                    "句尾「啦/哦」放软收、转述用「他跟我讲」",
        "avoid": "北方腔口头语（贼、咋整、儿化词）",
    },
    "beijing": {
        "label": "北京",
        "words": "倍儿（倍儿好）、得嘞、回头（回头说）、敢情、您猜怎么着、遛弯儿",
    },
}
# taiwan 档补句式/反腔（定义在上方基础三档，键就地补齐——保持单一定义点）
_DIALECT_PACKS["taiwan"]["words"] = (
    "蛮…的、超…的（超好吃）、有够…（有够扯）、诶、是喔、好了啦、"
    "真的假的、还不错诶、…的样子")
_DIALECT_PACKS["taiwan"]["patterns"] = (
    "「有够+形容词」「…的样子」收尾、附和用「对啊对啊/是喔」、"
    "句尾「啦/诶/喔」放软拖长半拍，整体语气偏软偏慢")
_DIALECT_PACKS["taiwan"]["avoid"] = (
    "北方腔口头语（贼、咋整、儿化词、我勒个豆、哇靠）——台湾词配北方语气会串成东北腔")


def dialect_style_line(flavor: str) -> str:
    """方言档 → 口语化 prompt 的风格指令；未知/空档/未出货 → ""。

    2026-08-30：闽普/乡音词表不再出货——没有对应语言模型时注入这些词会假装
    「说了闽南语」。``_DIALECT_PACKS`` 留档，``is_shipped_dialect`` 放行后才渲染。
    粤语不进本表（书面稿走 persona prompt + 声学路由）。
    """
    from src.ai.cosy_dialect import is_shipped_dialect
    f = str(flavor or "").strip().lower()
    if not is_shipped_dialect(f):
        return ""
    p = _DIALECT_PACKS.get(f)
    if not p:
        return ""
    line = (
        f"带一点{p['label']}口味说话：可自然用「{p['words']}」这类词，"
        "一条至多一两处、放在语气自然的位置，绝不堆砌；拿不准就不用；"
        "意思和事实必须与原文完全一致"
    )
    if p.get("patterns"):
        line += f"；句式上可用：{p['patterns']}"
    if p.get("avoid"):
        line += f"；绝不用{p['avoid']}"
    return line


def build_voice_style_hint(
    instruct_style: str = "",
    quirks: str = "",
    *,
    catchphrase: str = "",
    dialect: str = "",
) -> str:
    """拼 LLM 口语化用的语气/口头禅提示（instruct_style + quirks + 方言档合一）。"""
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
    d = dialect_style_line(dialect)
    if d:
        parts.append(d)
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


def _soft_laugh(
    text: str, emotion: str, seed: int, *, prob: float = 0.10,
) -> Tuple[str, bool]:
    """极轻开场笑意（单音节「嘿」）。已有哈/嘿开场或庄重情绪跳过。

    禁止注入「哈哈/哈哈哈」——TTS 念出来像假笑三连（听感事故 2026-07-24）。
    """
    variants = _SOFT_LAUGHS.get(emotion)
    if not variants:
        return text, False
    if _LAUGH_SKIP_RE.match(text):
        return text, False
    # 正文已自带笑点 → 不再句首加嘿（防「嘿，哈哈哈…」）
    if re.search(r"哈{2,}|嘿嘿|嘻嘻|笑死|太逗", text or ""):
        return text, False
    if ((seed >> 3) % 100) >= int(min(0.95, max(0.0, prob)) * 100):
        return text, False
    laugh = variants[(seed >> 19) % len(variants)]
    return f"{laugh}，{text.lstrip()}", True


def _thinking_repeat(
    text: str, seed: int, *, prob: float = 0.22, min_chars: int = 16,
) -> Tuple[str, bool]:
    """思考时重复一词：「我觉得可以」→「我觉得……我觉得可以」。

    只挑句中实义双字，避开虚词/连接；已含「X……X」形态则跳过（防叠加）。
    """
    core = str(text or "").strip()
    if len(core) < max(1, int(min_chars)):
        return text, False
    if "……" in core and re.search(r"([\u4e00-\u9fff]{2})……\1", core):
        return text, False
    if ((seed >> 11) % 100) >= int(min(0.95, max(0.0, prob)) * 100):
        return text, False
    cands: List[Tuple[int, str]] = []
    for m in _THINK_WORD_RE.finditer(core):
        w = m.group(0)
        if w in _THINK_SKIP:
            continue
        # 避开句首 0-1 字位置（太靠前像口吃开场）与句末 2 字
        if m.start() < 2 or m.end() > len(core) - 2:
            continue
        cands.append((m.start(), w))
    if not cands:
        return text, False
    pos, word = cands[(seed >> 21) % len(cands)]
    # 在该词首次出现处扩成「词……词」
    out = core[:pos] + f"{word}……{word}" + core[pos + len(word):]
    # 保留原文首尾空白形态（通常无）
    if text[:1].isspace() or text[-1:].isspace():
        return text.replace(core, out, 1) if core in text else out, True
    return out, True


_EN_VOCAL_LEADS = ("mm, ", "honestly, ")
_EN_LEAD_SKIP = (
    "mm", "mhm", "honestly", "anyway", "haha", "hah", "heh", "lol",
    "hey", "oh", "ahh", "aww",
)


def _colloquialize_english(
    text: str, *, spec: Any = None, enable_lead: bool = True,
    lead_prob: float = 0.4,
) -> str:
    """英文轻度口语化：只在句首加气声/口头禅，不加中文词、不改事实。"""
    t = str(text or "")
    core = t.strip()
    if not core:
        return t
    emotion, _ = normalize_colloquial_emotion(spec)
    if emotion in _FORMAL:
        return t
    low = core.lower()
    if any(low.startswith(p) for p in _EN_LEAD_SKIP):
        return t
    if not enable_lead or emotion not in ("warm", "playful", "happy", "calm", "neutral"):
        return t
    seed = zlib.crc32(core.encode("utf-8"))
    if (seed % 100) >= int(min(0.8, max(0.0, float(lead_prob))) * 100):
        return t
    lead = _EN_VOCAL_LEADS[seed % len(_EN_VOCAL_LEADS)]
    rest = core
    if rest[:1].isupper() and rest[:2] != "I " and rest[:3] != "I'm":
        rest = rest[:1].lower() + rest[1:]
    out = lead + rest
    if t[:1].isspace():
        return t[: len(t) - len(t.lstrip())] + out
    return out


def colloquialize(
    text: str,
    spec: Any = None,
    *,
    min_chars: int = 12,
    max_inserts: int = 2,
    enable_fillers: bool = True,
    enable_sentence_final: bool = False,
    enable_lexical: bool = True,
    enable_thinking_repeat: bool = False,
    enable_soft_laugh: bool = False,
    lead_prob: float = 0.55,
    final_prob: float = 0.4,
    think_prob: float = 0.22,
    laugh_prob: float = 0.18,
    persona_leads: Tuple[str, ...] = (),
) -> str:
    """把书面 TTS 文本轻度口语化 → 减「念稿感」。纯函数、确定性、防御式。

    仅作用于**送引擎合成的文本**，调用方须保住原文用于镜像/记忆（分眼睛/耳朵两版）。

    - ``min_chars``：短句 no-op 阈值（≤ 此长度原样返回，保预渲染命中 + 短句本就口语）。
    - ``max_inserts``：「加词」总量上限（句首迟疑 + 句末助词 + 轻笑/思考重复），词替换不占额度。
    - ``enable_*``：手段分别可关；``thinking_repeat``/``soft_laugh`` 默认关（生产由
      ``colloquial.human_ticks`` 打开）。
    - 情绪门控：serious/apologetic 只做 SAFE 词替换；sad/empathetic 句首让位副语言标记。
    - neutral 也改写（日常对话主路，最需要文字层活人感）。
    - 真人微特征互斥：轻笑与思考重复同条至多一种（seed 奇偶分流），防做作。

    不适用（空/短/异常）→ 原文不变。
    """
    try:
        t = str(text or "")
        core = t.strip()
        if len(core) < max(1, int(min_chars)):
            return t                        # 短句 no-op（保预渲染 + 短句本就口语）
        if _is_english_dominant(core):
            return _colloquialize_english(
                t, spec=spec, enable_lead=enable_fillers,
                lead_prob=lead_prob)
        if not _is_chinese_dominant(core):
            return t                        # 其它外语 → 跳过（中文口语词会 garble）
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
        # 3) 真人微特征（占 1 额度，互斥）：轻笑优先开心档；思考重复偏中性/暖
        if inserts < cap and not formal:
            use_laugh = bool(enable_soft_laugh) and emotion in _SOFT_LAUGHS
            use_think = bool(enable_thinking_repeat)
            # seed 奇偶分流：避免同条又笑又结巴
            prefer_laugh = (seed & 1) == 0
            if use_laugh and (prefer_laugh or not use_think):
                out, hit = _soft_laugh(out, emotion, seed, prob=laugh_prob)
                if hit:
                    inserts += 1
                    use_think = False
            if use_think and inserts < cap:
                out, hit = _thinking_repeat(
                    out, seed, prob=think_prob, min_chars=max(16, int(min_chars)))
                if hit:
                    inserts += 1
        # 4) 句末语气助词（占 1 额度，默认关）：仅暖/俏皮/开心
        if enable_sentence_final and inserts < cap and not formal:
            out, hit = _sentence_final(out, emotion, seed, prob=final_prob)
            if hit:
                inserts += 1
        return out
    except Exception:
        return str(text or "")


__all__ = [
    "colloquialize", "normalize_colloquial_emotion",
    "_soft_laugh", "_thinking_repeat",
]
