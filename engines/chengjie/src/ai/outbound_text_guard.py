# -*- coding: utf-8 -*-
"""出站文本形态守卫（实施74 阶段1：B118 内心独白 + B121 语种混杂）。

两类「一眼穿帮」出站事故的确定性最后防线（生成端 prompt 硬禁是第一道，
本模块是出稿口兜底；A 线 process_message / B 线 generate_inbox_draft 经
``SkillManager._apply_outbound_text_guard`` 同口径接线）：

- **B118**（0826 ``_578`` 实录，用户 ``_582`` 定调「废除内心独白」）：WhatsApp
  全自动把括号内心独白当正文直发——客户「我只是生你的气」→ AI 发
  「（我没生气，就是心里堵得慌）」，暴露 AI 风险。
- **B121**（0826 ``_590`` 实录，P1 加急）：出站英文混汉字
  「I'm 我 going anywhere.」直发客户。

设计原则（与 persona_guard / reply_echo_guard 同族）：

- **纯函数、确定性、零 LLM**——守卫位不做 LLM 重写（延迟 + 失败面；两起实录
  案的确定性变换都能产出合法台词：整段包裹 → 去壳保台词；混语 → 剥少数派
  字符）。「重生成」语义由生成端硬禁（persona_manager 双模式 prompt 行）承担
  第一道，本模块保证**坏形态绝不出站**。
- **宁可漏拦不误伤**：正常括号补充语（「我买了新手机（iPhone）」）不动；
  中文句夹常见英文词（品牌名/OK/gym）不判混语；中文主体夹整句英文只观测
  不动手（语言教学等场景合法）。
- 清洗后为空一律回退原文，绝不因守卫吞掉回复。

配置 ``companion.outbound_text_guard.{enabled, monologue, lang_mix}``——
暴露风险守卫族**默认开**（与 media_promise_guard 同约定），``enabled: false``
一键回旧行为。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# ── 括号旁白（B118） ─────────────────────────────────────────────────────────

# 全/半角括号 + 方括号；成对捕获（不跨行、限长防灾难回溯）
_BRACKET_RES = [
    re.compile(r"（([^（）\n]{1,80})）"),
    re.compile(r"\(([^()\n]{1,80})\)"),
    re.compile(r"【([^【】\n]{1,80})】"),
    re.compile(r"\[([^\[\]\n]{1,80})\]"),
]
# 单星号动作体 *叹了口气*（避开 **markdown 粗体**）
_ASTERISK_RE = re.compile(r"(?<!\*)\*([^*\n]{1,60})\*(?!\*)")

# 旁白/动作/心理活动标记词（中文按子串、英文按词边界）——刻意收窄到
# 「几乎不可能是正文补充语」的动作/心理词，防误伤正常括号注释。
# #71（0830，B118 英文变体实锤「(tone shifts to playful)」直发客户）：补齐
# 中英「语气/舞台指示」全家族——语气切换、声线描述、方式副词、眼神动作。
_NARRATION_ZH = (
    "叹气", "叹了", "轻叹", "微笑", "苦笑", "傻笑", "偷笑", "大笑", "轻笑",
    "沉默", "摇头", "点头", "皱眉", "挠头", "耸肩", "翻白眼", "看着", "望着",
    "盯着", "凑近", "靠近", "深呼吸", "哽咽", "抹泪", "擦泪", "撇嘴", "嘟嘴",
    "脸红", "心里", "心想", "内心", "自言自语", "小声", "低声", "停顿",
    "沉思", "想了想", "环顾", "装作", "假装", "扶额", "捂脸", "揉了揉",
    # ── #71 扩充：语气/声线/亲昵动作类舞台指示 ──
    "语气", "口吻", "语调", "声音放", "压低声", "轻声", "撒娇", "宠溺",
    "坏笑", "挑眉", "眨眼", "眨了眨", "歪头", "咬唇", "咬着唇", "清了清嗓",
    "清嗓", "凑到", "贴近", "撅嘴", "别过头", "移开视线", "对视",
)
_NARRATION_ZH_SHORT = {"笑", "哭", "叹", "汗", "尬", "无奈", "害羞", "委屈",
                       "温柔", "调皮", "俏皮"}
_NARRATION_EN = re.compile(
    r"\b(sighs?|smiles?|smiling|laughs?|laughing|nods?|nodding|pauses?|"
    r"whispers?|whispering|blushes|blushing|shrugs?|chuckles?|grins?|"
    # ── #71 扩充：语气切换/声线（实锤形态 "(tone shifts to playful)"） ──
    r"tone (?:shifts?|softens?|turns?|changes?|drops?|becomes?)|"
    r"shifts? (?:to|into) \w+|"
    r"voice (?:softens?|drops?|lowers?|trembl\w*|breaks?)|"
    r"(?:in|with) an? \w+ (?:tone|voice)|"
    # ── #71 扩充：常见动作/眼神舞台指示 ──
    r"giggles?|giggling|winks?|winking|smirks?|smirking|"
    r"leans? (?:in|closer|back|forward)|rolls? (?:his |her |their )?eyes|"
    r"bites? (?:his |her |their )?lips?|"
    r"raises? (?:an |his |her |their )?eyebrows?|"
    r"tilts? (?:his |her |their )?head|clears? (?:his |her |their )?throat|"
    r"takes? a deep breath|deep breath|"
    r"eyes (?:widen|light up|sparkle|narrow)|"
    r"looks? (?:away|down|up|at you)|stares?|gazes?|"
    # ── #71 扩充：方式副词（独立成旁白时几乎必是舞台指示） ──
    r"playfully|teasingly|jokingly|sarcastically|nervously|shyly|"
    r"dramatically|hesitantly|mischievously|sheepishly|softly|gently|"
    r"thinking|internally|to (?:him|her)self)\b",
    re.IGNORECASE,
)


def _is_narration(inner: str) -> bool:
    s = (inner or "").strip()
    if not s:
        return False
    if s in _NARRATION_ZH_SHORT:
        return True
    if any(m in s for m in _NARRATION_ZH):
        return True
    if _NARRATION_EN.search(s):
        return True
    return False


def _whole_wrapped(text: str) -> Optional[str]:
    """整条回复被单层括号包裹 → 返回去壳后的台词；否则 None。

    「（我没生气，就是心里堵得慌）」→「我没生气，就是心里堵得慌」。
    内容里再出现同对括号（如「(a) 正文 (b)」）不算整段包裹。
    """
    t = (text or "").strip()
    pairs = [("（", "）"), ("(", ")"), ("【", "】"), ("[", "]")]
    for op, cl in pairs:
        if len(t) >= 2 and t.startswith(op) and t.endswith(cl):
            inner = t[1:-1]
            if op not in inner and cl not in inner and inner.strip():
                return inner.strip()
    return None


def _cleanup_spacing(text: str) -> str:
    out = re.sub(r"[ \t]{2,}", " ", text)
    # 剥除后残留的「空格+标点」缝隙（如「好啦 ，明天见」）
    out = re.sub(r" +([，。！？、,.!?;；])", r"\1", out)
    return out.strip()


def sanitize_inner_monologue(text: str) -> Tuple[str, List[str]]:
    """剥除括号/星号包裹的旁白·内心独白，返回 (清洗后文本, 命中片段)。

    - 整条被括号包裹 → 去壳保台词（内容本身多半是合法台词，只是括号让它
      变成了独白——``_578`` 实录正是此形态）；
    - 内嵌片段仅在命中旁白标记词时剥除（保守，防误伤正常补充语）；
    - 清洗后为空 → 回退原文。
    """
    src = text or ""
    if not src.strip():
        return src, []
    hits: List[str] = []

    unwrapped = _whole_wrapped(src)
    if unwrapped is not None:
        hits.append(src.strip())
        return unwrapped, hits

    out = src
    for rx in _BRACKET_RES:
        def _sub(m: "re.Match[str]") -> str:
            if _is_narration(m.group(1)):
                hits.append(m.group(0))
                return " "
            return m.group(0)
        out = rx.sub(_sub, out)
    out = _ASTERISK_RE.sub(
        lambda m: (hits.append(m.group(0)) or " ") if _is_narration(m.group(1))
        else m.group(0),
        out,
    )
    out = _cleanup_spacing(out)
    if not out:
        return src, hits
    return out, hits


# ── 语种混杂（B121） ─────────────────────────────────────────────────────────

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
# \w 取 Python 默认 Unicode 语义：@小红 / @user_名字 这类含 CJK 的 handle
# 同样整体豁免（handle 是引用不是语种混杂）。
_HANDLE_RE = re.compile(r"@[\w.]+")
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’-]*")


def _analysis_text(text: str) -> str:
    out = _URL_RE.sub(" ", text or "")
    out = _EMAIL_RE.sub(" ", out)
    out = _HANDLE_RE.sub(" ", out)
    return out


def detect_lang_mix(text: str) -> Dict[str, Any]:
    """检出出站文本的语种混杂。

    返回 ``{mixed, dominant, action}``：

    - 拉丁主体（字母 ≥6 且 ≥3× CJK 字数）夹任何 CJK → ``action="hard"``
      （事故方向：英文句夹汉字，确定性剥除少数派）；
    - CJK 主体（≥8 字）夹 **连续 ≥5 个英文词** 的整句 → ``action="soft"``
      （只观测不动手——中文里引用整句英文有合法场景）；
    - 其余（含中文夹品牌词/短英文）→ 不判。

    URL / email / @handle 先行豁免，不参与统计。
    """
    a = _analysis_text(text or "")
    cjk = len(_CJK_RE.findall(a))
    latin = len(_LATIN_RE.findall(a))
    if latin >= 6 and cjk >= 1 and latin >= 3 * cjk:
        return {"mixed": True, "dominant": "latin", "action": "hard"}
    if cjk >= 8 and latin >= 10:
        words = _LATIN_WORD_RE.findall(a)
        # 找连续英文词 run：按原文顺序，非字母段打断
        run = 0
        best = 0
        for seg in re.split(r"[^A-Za-z'’-]+", a):
            if _LATIN_WORD_RE.fullmatch(seg or ""):
                run += 1
                best = max(best, run)
            else:
                run = 0
        if best >= 5 and len(words) >= 5:
            return {"mixed": True, "dominant": "cjk", "action": "soft"}
    return {"mixed": False, "dominant": "", "action": ""}


def strip_minority_script(text: str) -> str:
    """拉丁主体句剥除 CJK 字符（含紧邻的全角标点缝隙清理）。

    「I'm 我 going anywhere.」→「I'm going anywhere.」
    安全阀：剥后拉丁字母 <4 或不足原一半 → 返回原文（宁可保留可疑原文，
    也不出站残句）。
    """
    src = text or ""
    before_latin = len(_LATIN_RE.findall(src))
    out = _CJK_RE.sub(" ", src)
    out = re.sub(r"[\u3000-\u303f\uff01-\uff65]", " ", out)  # 全角标点随 CJK 走
    out = _cleanup_spacing(out)
    after_latin = len(_LATIN_RE.findall(out))
    if after_latin < 4 or after_latin * 2 < before_latin:
        return src
    return out


# ── 无出处引用（B104，实施74 二批） ─────────────────────────────────────────

# 「引用过往对话」的措辞（宁可漏拦：**刻意不收**裸「你说过」——会误命中
# 「你说过年回家吗」；不收「你刚(才)说」——同会话内引用上一条是合法的）。
_RECALL_ZH = (
    "你之前说过", "你之前提到", "你之前不是说", "像你之前说的",
    "之前你说过", "之前你提到",
    "你上次说", "你上次提到", "上次你说", "上次你提到",
    "你跟我说过", "你和我说过", "你不是说过", "记得你说过", "你曾说过",
    "如你所说", "正如你说过",
)
_RECALL_EN = re.compile(
    r"\b(as you (?:said|mentioned)(?: before| last time)?|"
    r"you (?:said|mentioned|told me)(?: that)?\s+(?:before|last time|earlier)|"
    r"remember (?:when )?you (?:said|told me)|"
    r"like you (?:said|mentioned) (?:before|last time))\b",
    re.IGNORECASE,
)
# 「几乎没聊过」的轮数上限：0 个用户轮（对**首条**消息的回复）时任何过往
# 引用必为编造；≥1 轮起「你之前说过」可能合法指向本会话更早消息（实测 LLM
# 会这么措辞），一律放行——宁可漏拦不误伤。
_RECALL_MAX_TURNS = 0
_SENT_DELIMS = "。！？!?；;\n"


def _split_sentences(text: str) -> List[str]:
    """句级切分（含界符）：CJK 句界直切；拉丁句号只在后随空白/行尾时算句界
    （保护 3.5 / example.com 不被拦腰切）。"""
    segs: List[str] = []
    buf: List[str] = []
    n = len(text)
    for i, ch in enumerate(text):
        buf.append(ch)
        if ch in _SENT_DELIMS or (
                ch == "." and (i + 1 == n or text[i + 1] in " \t\r\n")):
            segs.append("".join(buf))
            buf = []
    if buf:
        segs.append("".join(buf))
    return segs


def _has_recall_phrase(sentence: str) -> bool:
    s = sentence or ""
    if any(p in s for p in _RECALL_ZH):
        return True
    return bool(_RECALL_EN.search(s))


def strip_unfounded_recall(
    text: str, *, user_turns: Optional[int], has_memory: bool,
) -> Tuple[str, List[str]]:
    """B104（实录：全新联系人被 AI「你之前说过…」）：无出处引用剥句。

    仅当「用户轮数已知且 ≤ ``_RECALL_MAX_TURNS``（0＝首答）」且「无长期
    记忆」才动手——老客户/有记忆/轮数未知（含历史被压缩成摘要的老会话）/
    会话已有来回，一律原样放行；命中句整句剥除，剥空回退原文（绝不因守卫
    吞掉回复，第一防线仍是生成端上下文）。
    """
    src = text or ""
    if not src.strip():
        return src, []
    if has_memory or user_turns is None or int(user_turns) > _RECALL_MAX_TURNS:
        return src, []
    hits: List[str] = []
    kept: List[str] = []
    for seg in _split_sentences(src):
        if not seg:
            continue
        if seg.strip() and _has_recall_phrase(seg):
            hits.append(seg.strip())
        else:
            kept.append(seg)
    if not hits:
        return src, []
    out = _cleanup_spacing("".join(kept))
    if not out:
        return src, hits
    return out, hits


# ── 回忆类断言接地锁（#91，实施90 二批） ─────────────────────────────────────
#
# 实锤（0830 钧，会话十翼，工单 #91 已定性纯幻觉）：记忆库全空、历史无一字
# 提过电脑，AI 当轮现编「你上次提过那台惠普OMEN电竞系列吗」还自夸「我记性
# 可好了」。B104 只拦「首答无记忆」的新联系人；本锁覆盖**全部轮次**：凡是
# 「你上次提过/说过 X」式回忆断言，X 必须与**用户侧历史原话或记忆条目**有
# 内容级词汇重叠（CJK bigram / 拉丁词 / 数字，与 Phase8 memory_grounding
# 同一口径）——对不上＝当轮现编，整句剥除。
#
# 保守设计（与记忆接地同哲学「宁可漏记不可错记」，此处=宁可不提不许编）：
# - 断言内容取不出内容 token（超短）→ 放行（无从判断）；
# - 接地语料为空（历史没传进来）→ 整体不动手（守卫缺料绝不乱杀）；
# - 通用功能 bigram（那台/这个/时候…）不算接地证据——防「用户说过『那台』」
#   这类停用词碰瓷让幻觉溜过；剔完没有内容 token 的断言同样放行。
# 与 B104 的分工：B104 管「首答任何回忆措辞必编造」（零语料也拦），本锁管
# 「有历史但断言内容对不上」；两者共用句级剥除与回退纪律。

# 回忆断言标记（比 B104 的 _RECALL_ZH 宽：补「提过/发过/聊过」与「你说过你」
# 形——B104 刻意窄是因为它零语料一刀切，本锁有接地校验兜底，可以宽进严出）
_RECALL_CLAIM_ZH = _RECALL_ZH + (
    "你上次提过", "你之前提过", "你提到过", "你提过", "你说过你", "你说过想",
    "你发过", "你聊过", "你不是提过", "你跟我提过", "你和我提过",
    "记得你提过", "你曾提过", "你那天说", "你那天提",
)
_RECALL_CLAIM_EN = re.compile(
    r"\b(you (?:mentioned|said|told me|talked about)\b[^,.!?\n]{0,60}"
    r"\b(?:before|last time|earlier|the other day)|"
    r"(?:last time|earlier|the other day)[^,.!?\n]{0,20}"
    r"\byou (?:mentioned|said|told me)|"
    r"you once (?:mentioned|said|told me)|"
    r"didn'?t you (?:say|mention|tell me))\b",
    re.IGNORECASE,
)
# 英文标记**词**（供内容抽取挖除——检测正则带内容窗口，全挖会把断言内容
# 一起挖没 → 无从接地；时间指涉词单列同挖，它们不是断言内容）。
_RECALL_EN_MARKER_SPAN_RE = re.compile(
    r"\b(?:you (?:mentioned|said|told me|talked about)(?:\s+that)?|"
    r"you once (?:mentioned|said|told me)|"
    r"didn'?t you (?:say|mention|tell me)|"
    r"as you (?:said|mentioned)|"
    r"remember (?:when )?you (?:said|told me|mentioned)|"
    r"before|last time|earlier|the other day)\b",
    re.IGNORECASE,
)

# 断言内容里不算「接地证据」的通用 bigram（功能词/指代词/时间词——用户历史里
# 几乎必然出现过，拿它们当锚等于不设防）。刻意小而保守：删多了会把合法回忆
# 误判成幻觉（token 剔光=放行，故这个表偏大时反而是放行偏置，安全）。
_GENERIC_BIGRAMS = frozenset({
    "那台", "那个", "这个", "这些", "那些", "什么", "时候", "现在", "今天",
    "明天", "昨天", "上次", "之前", "以前", "后来", "东西", "事情", "还是",
    "就是", "不是", "可以", "喜欢", "觉得", "知道", "咱们", "我们", "你们",
    "怎么", "这样", "那样", "有点", "一下", "一个", "对吧", "是不",
})


def _recall_marker_spans(sentence: str) -> List[Tuple[int, int]]:
    """句内全部回忆标记的 (start, end) 区间（zh 子串 + en 标记词正则）。"""
    s = sentence or ""
    spans: List[Tuple[int, int]] = []
    for p in _RECALL_CLAIM_ZH:
        start = 0
        while True:
            i = s.find(p, start)
            if i < 0:
                break
            spans.append((i, i + len(p)))
            start = i + len(p)
    for m in _RECALL_EN_MARKER_SPAN_RE.finditer(s):
        spans.append((m.start(), m.end()))
    return spans


def _claim_content(sentence: str) -> str:
    """剥掉回忆标记后的断言内容（用于接地校验）。"""
    s = sentence or ""
    spans = sorted(_recall_marker_spans(s))
    if not spans:
        return s
    out: List[str] = []
    prev = 0
    for a, b in spans:
        if a > prev:
            out.append(s[prev:a])
        prev = max(prev, b)
    out.append(s[prev:])
    return " ".join(x for x in out if x)


def _has_recall_claim(sentence: str) -> bool:
    s = sentence or ""
    if any(p in s for p in _RECALL_CLAIM_ZH):
        return True
    return bool(_RECALL_CLAIM_EN.search(s))


def _grounding_tokens(text: str) -> Tuple[set, set]:
    """接地 token（CJK bigram / 拉丁词+数字），复用 Phase8 记忆接地同一口径。"""
    try:
        from src.ai.memory_grounding import _content_tokens
        return _content_tokens(text)
    except Exception:
        return set(), set()


def strip_hallucinated_recall(
    text: str, *,
    user_texts: Optional[List[str]] = None,
    memory_text: str = "",
) -> Tuple[str, List[str]]:
    """#91-A：回忆类断言接地锁——「你上次提过X」对不上历史/记忆即整句剥除。

    ``user_texts``＝用户侧历史消息文本；``memory_text``＝情景记忆块整文。
    语料两者皆空 → 不动手（缺料不乱杀）；命中句剥空回退原文。
    """
    src = text or ""
    if not src.strip():
        return src, []
    corpus_parts = [str(t) for t in (user_texts or []) if str(t or "").strip()]
    if str(memory_text or "").strip():
        corpus_parts.append(str(memory_text))
    if not corpus_parts:
        return src, []
    c_bi, c_latin = _grounding_tokens("\n".join(corpus_parts))
    if not c_bi and not c_latin:
        return src, []
    hits: List[str] = []
    kept: List[str] = []
    for seg in _split_sentences(src):
        if not seg:
            continue
        if not _has_recall_claim(seg):
            kept.append(seg)
            continue
        claim = _claim_content(seg)
        f_bi, f_latin = _grounding_tokens(claim)
        f_bi = f_bi - _GENERIC_BIGRAMS
        if not f_bi and not f_latin:
            kept.append(seg)          # 断言无实词 → 无从判断，放行
            continue
        if (f_bi & c_bi) or (f_latin & c_latin):
            kept.append(seg)          # 对得上原话/记忆 → 合法回忆
            continue
        hits.append(seg.strip())      # 现编断言 → 整句剥除
    if not hits:
        return src, []
    out = _cleanup_spacing("".join(kept))
    if not out:
        return src, hits
    return out, hits


# ── #110 共同经历叙事接地锁（实施91，0831 原图 880 四连实锤）──────────────────
#
# #91-A 只拦「你说过/提过X」**引用式**主张；叙事式虚构（「我们一起在河边那家
# 小海鲜店…老板一直给我们续杯」「你吃了烤大虾，我发誓你还吃了我的一半」）
# 没有引用句形，整段绕过——同为无接地记忆主张，本锁把检测面扩到「宣称共同
# 经历/共同事件」。两档处置：
# ① **关系阶段闸**（最廉价确定）：stage=initial/warming（初识/试探）——刚认识
#   不存在任何共同过去，物理世界共同经历叙事一律剥（聊天指涉「我们上次聊到」
#   已被句形排除，不误伤）；
# ② 阶段更深/未知：与 #91-A 同一套接地校验（句内容 token 对不上历史/记忆
#   → 现编，剥句）。语料两者皆空且非早期阶段 → 不动手（缺料不乱杀）。
# 句形宁窄勿宽：高频关怀问句（「你吃了吗/你吃了饭没」）、将来邀约（「下次
# 我们一起去」）、聊天指涉（「我们上次聊到」）都在句形层排除。

_SHARED_PAST_ZH_RE = re.compile(
    # 我们一起去过/吃过/看过…（明确共同过去动作）
    r"我们(?:一起|俩|两个人)?(?:去|吃|喝|看|逛|玩|见|住|待)过"
    # 我们上次/那天/那晚…（过去时点锚 + 具体行为动词；聊/说不收=聊天指涉合法）
    r"|我们(?:上次|那天|那晚|那次|当时)(?:一起)?[^。！？!?\n]{0,6}(?:去|吃|喝|看|逛|玩|坐)"
    # 还记得我们/那家…（唤起共同记忆）
    r"|(?:你还记得|还记得|记得)(?:我们|咱们|那)[^。！？!?\n]{0,10}(?:一起|那家|那晚|那次|店|餐厅|馆)"
    # 第三方服务我们（老板给我们续杯——事故原形①）
    r"|(?:老板|服务员|店家|老板娘)[^。！？!?\n]{0,10}给(?:我们|咱们)"
    # 重返老地方（再坐在那张同样的桌子旁——事故原形④）
    r"|再(?:去|坐在?|回到)[^。！？!?\n]{0,12}(?:同样|那家|那间|那张|老地方)"
    # 补叙式共同用餐（你还吃了我的一半——事故原形②；裸「你吃了」刻意不收，
    # 「你吃了吗/你吃了饭没」是最高频关怀问句）
    r"|你还(?:吃|喝|点|尝)了"
    r"|我发誓你[^。！？!?\n]{0,12}(?:吃|喝|点|拿)"
    # 追忆过去时点（我仍然会想起那晚——事故原形③）
    r"|想起(?:那晚|那天|那次)"
    r"|想起(?:我们|咱们)[^。！？!?\n]{0,10}(?:一起|去|吃|喝|看|玩)"
)
_SHARED_PAST_EN_RE = re.compile(
    r"\bremember (?:when|that time) we\b"
    r"|\bwe (?:went|ate|visited|sat)\b[^,.!?\n]{0,40}"
    r"\b(?:together|that (?:night|day|evening)|last (?:time|week|month))\b"
    r"|\bthat (?:night|evening|day) we\b"
    r"|\bwe had\b[^,.!?\n]{0,30}\bthat (?:night|day|evening)\b"
    r"|\b(?:sit|sat) at the same table\b"
    r"|\bwe used to\b",
    re.IGNORECASE,
)
# 早期关系阶段（共同物理过去按定义不存在）——companion_relationship.STAGE_ORDER
# 前两档；stage 值经 str 归一，未知/空不算早期（宁可走接地档不误杀）。
_EARLY_STAGES = frozenset({"initial", "warming"})


def _has_shared_past_claim(sentence: str) -> bool:
    s = sentence or ""
    return bool(_SHARED_PAST_ZH_RE.search(s) or _SHARED_PAST_EN_RE.search(s))


def strip_ungrounded_shared_past(
    text: str, *,
    user_texts: Optional[List[str]] = None,
    memory_text: str = "",
    relationship_stage: str = "",
) -> Tuple[str, List[str]]:
    """#110：共同经历叙事接地锁。返回 ``(out, hits)``；命中句剥空回退原文。

    早期阶段（initial/warming）命中即剥（无需语料）；其余阶段走接地校验，
    语料缺席不动手。
    """
    src = text or ""
    if not src.strip():
        return src, []
    stage = str(relationship_stage or "").strip().lower()
    early = stage in _EARLY_STAGES
    corpus_parts = [str(t) for t in (user_texts or []) if str(t or "").strip()]
    if str(memory_text or "").strip():
        corpus_parts.append(str(memory_text))
    c_bi: set = set()
    c_latin: set = set()
    if corpus_parts:
        c_bi, c_latin = _grounding_tokens("\n".join(corpus_parts))
    if not early and not (c_bi or c_latin):
        return src, []          # 非早期且无语料 → 缺料不乱杀
    hits: List[str] = []
    kept: List[str] = []
    for seg in _split_sentences(src):
        if not seg:
            continue
        if not _has_shared_past_claim(seg):
            kept.append(seg)
            continue
        if early:
            hits.append(seg.strip())   # 初识/试探期：共同过去按定义不存在
            continue
        f_bi, f_latin = _grounding_tokens(seg)
        f_bi = f_bi - _GENERIC_BIGRAMS
        if not f_bi and not f_latin:
            kept.append(seg)
            continue
        if (f_bi & c_bi) or (f_latin & c_latin):
            kept.append(seg)           # 对得上历史/记忆 → 真实共同语境
            continue
        hits.append(seg.strip())
    if not hits:
        return src, []
    out = _cleanup_spacing("".join(kept))
    if not out:
        return src, hits
    return out, hits


# ── 认错口癖去重（#91-C） ────────────────────────────────────────────────────
#
# 实锤（同单 C 层）：「哎呀被你抓包了」被测试人点名「AI犯错都会讲」——认错
# 话术单一模板复读，错一次说一次，油腻感直接出戏。**历史即状态**：近几条
# assistant 消息里已用过同款口癖 → 本条换变体（确定性轮换，选近史里没出现
# 过的；变体池确保句子结构完整，绝不裸剥留残句）。零新存储、纯函数。

_APOLOGY_FAMILIES: List[Tuple[str, "re.Pattern[str]", Tuple[str, ...]]] = [
    ("zhuabao",
     re.compile(r"被你(?:抓包|逮到|逮个正着)(?:了)?[啦哈呀嘛~～]?"),
     ("让你说着了", "好吧我承认", "行吧，瞒不过你", "我认栽")),
    ("faxian",
     re.compile(r"被你发现(?:了)?[啦哈呀嘛~～]?"),
     ("让你看出来了", "好吧藏不住", "行吧，逃不过你的眼睛")),
]
_APOLOGY_RECENT_WINDOW = 8   # 只看最近 N 条 assistant 消息


def dedup_apology_catchphrase(
    text: str, recent_assistant_texts: Optional[List[str]] = None,
) -> Tuple[str, List[str]]:
    """#91-C：同会话认错口癖去重——近史用过的口癖换确定性变体。

    返回 ``(out, hits)``；hits＝被替换的原口癖片段。近史缺席/未复读原样返回。
    """
    src = text or ""
    if not src.strip():
        return src, []
    recent = [str(t) for t in (recent_assistant_texts or [])
              if str(t or "").strip()][-_APOLOGY_RECENT_WINDOW:]
    if not recent:
        return src, []
    recent_blob = "\n".join(recent)
    hits: List[str] = []
    out = src
    for _fam, rx, variants in _APOLOGY_FAMILIES:
        m = rx.search(out)
        if not m:
            continue
        if not rx.search(recent_blob):
            continue          # 近史没用过 → 本条是首次，合法
        # 变体选择：先挑近史没出现过的；全出现过按 crc32(文本) 确定性轮换
        # （确定性=同稿重跑同结果，缓存/重试友好）。
        unused = [v for v in variants if v not in recent_blob]
        pool = unused or list(variants)
        import zlib
        pick = pool[zlib.crc32(src.encode("utf-8", "ignore")) % len(pool)]
        hits.append(m.group(0))
        out = out[:m.start()] + pick + out[m.end():]
    if not hits:
        return src, []
    out = _cleanup_spacing(out)
    return (out or src), hits


# ── #109 第4病：目标话术渗味（步骤感元话术）────────────────────────────────
#
# 实锤（0831 skuio 原图，#111 同屏追问）：目标引擎给 AI 的推进 agenda 是内部
# 剧本，AI 却把「过程语言」说给了客户——"Glad you're still interested — that's
# the first step."（工作台中译「很高兴你还对这个感兴趣——这是第一步」）。客户
# 视角这是「把我当项目推进」的出戏话术。确定性软校验：命中「步骤/里程碑/离
# 目标近一步」类元话术 → 剥除该**子段**（逗号/破折号切分，保留同句其余内容），
# 整句皆元话术才整句剥；剥空回退原文。词表刻意窄（宁可漏拦不误伤）：
# 「下一步去哪玩」这类日常用语绝不能命中，故不收裸「下一步/next step」。

_GOAL_META_RES = [
    re.compile(
        r"(?:that|this)(?:'|\u2019)?s\s+(?:just\s+)?(?:the|our)\s+first\s+step"
        r"|(?:that|this)\s+is\s+(?:just\s+)?(?:the|our)\s+first\s+step"
        r"|\bthe\s+first\s+step\s+(?:of|in|towards?)\b"
        r"|\bstep\s+(?:one|1)\s+(?:is|of|complete[d]?)\b"
        r"|\b(?:first|next)\s+milestone\b"
        r"|\bone\s+step\s+closer\s+to\s+(?:my|our|the)\s+goal\b",
        re.IGNORECASE),
    re.compile(
        r"这(?:只|就)?[是算](?:我们)?的?第一步"
        r"|第一步(?:已(?:经)?)?(?:达成|完成)"
        r"|离(?:我们的?)?目标(?:又)?近了一步"
        r"|(?:第一|下一)个里程碑"),
]
_GOAL_META_SEG_SPLIT = re.compile(r"([，,;；:：]|——|—|…{1,2}|\.{3})")
_GOAL_META_SENT_SPLIT = re.compile(r"(?<=[。！？!?\n])|(?<=[.](?=\s))")


def _goal_meta_hit(seg: str) -> Optional[str]:
    for rx in _GOAL_META_RES:
        m = rx.search(seg)
        if m:
            return m.group(0)
    return None


def strip_goal_meta_talk(text: str) -> Tuple[str, List[str]]:
    """剥除步骤感元话术子段（确定性、零 LLM、绝不抛；剥空回退原文）。"""
    src = text or ""
    try:
        if not src.strip():
            return src, []
        hits: List[str] = []
        out_sents: List[str] = []
        for sent in [s for s in _GOAL_META_SENT_SPLIT.split(src) if s]:
            if not _goal_meta_hit(sent):
                out_sents.append(sent)
                continue
            # 子段级剥除：只丢命中的子段，保住同句里的正常内容
            # （「很高兴你还对这个感兴趣——这是第一步。」只剥破折号后半）。
            parts = _GOAL_META_SEG_SPLIT.split(sent)
            kept: List[str] = []
            pend_sep = ""
            for p in parts:
                if _GOAL_META_SEG_SPLIT.fullmatch(p or ""):
                    pend_sep = p
                    continue
                h = _goal_meta_hit(p)
                if h:
                    hits.append(h)
                    pend_sep = ""
                    continue
                if p.strip():
                    if kept and pend_sep:
                        kept.append(pend_sep)
                    kept.append(p)
                pend_sep = ""
            rebuilt = "".join(kept).strip()
            if rebuilt:
                # 句尾标点跟着原句走（子段剥除可能把「……第一步。」的句号剥没）
                tail = sent.rstrip()[-1:] if sent.rstrip()[-1:] in "。！？!?." else ""
                if tail and not rebuilt.endswith(tuple("。！？!?.")):
                    rebuilt += tail
                out_sents.append(rebuilt)
            # 整句皆元话术 → 整句剥（out_sents 不收）
        if not hits:
            return src, []
        out = _cleanup_spacing("".join(out_sents))
        return (out if (out or "").strip() else src), hits
    except Exception:
        return src, []


# ── 出站收口点混语兜底（#97，实施91） ────────────────────────────────────────
#
# 击穿实锤（0830 21:47，v1.0.63 已带 #64 修复仍出「I'm 我 the one who's still
# here…」）：#64 把守卫挂在 A/B 出稿口 + 三条**翻译**出口，但主动触达/关怀/
# 唤醒等 deferred 链的文案不经出稿口、英文客户又不触发翻译（「已是客户语言即
# 跳过不译」）→ 整条 orch.send 直发路径裸奔。修法＝把确定性混语兜底装到
# **全平台全链共过的 send 收口点**（AccountOrchestrator.send / A 线
# sender._send_reply 经 outbound_quality_pass）——无论文本从哪条链来、中途被
# 谁改写过，出门前必过这一道。
#
# 分层契约（与 #64 架构互补，不替代）：LLM 重写档在**生成端**
# （ai_client._guard_reply_language：检出→重写→复检），收口点只做确定性剥除
# ——send 热路径绝不挂 LLM（延迟 + 失败面；本模块头部设计原则第 1 条）。
# 复检恒成立：hard 剥除按定义清空全部 CJK，唯一失败面是「剥后过短拒剥」
# （hard_kept，如裸「im 我」），此时保留原文出站与 #64 行为一致（有测试钉住）。
# **人工手打文本绝不动**——坐席刻意中英混写是人的表达（origin=manual 由调用方
# 把关不进本函数）。


def sendpoint_lang_mix_pass(text: str) -> Tuple[str, str]:
    """出站收口点混语兜底（确定性、零 LLM、绝不抛）。

    返回 ``(应发送文本, action)``；action ∈ ``""``（未命中/守卫不动手）、
    ``"hard_stripped"``（拉丁主体夹 CJK 已剥）、``"hard_kept"``（命中但剥后
    过短，保留原文）、``"soft"``（CJK 主体夹整句英文，只观测）。
    """
    src = text or ""
    try:
        if not src.strip():
            return src, ""
        verdict = detect_lang_mix(src)
        action = str(verdict.get("action") or "")
        if action == "hard":
            stripped = strip_minority_script(src)
            if stripped != src and stripped.strip():
                _STATS["sendpoint_hard"] += 1
                return stripped, "hard_stripped"
            _STATS["sendpoint_kept"] += 1
            return src, "hard_kept"
        if action == "soft":
            _STATS["sendpoint_soft"] += 1
            return src, "soft"
        return src, ""
    except Exception:
        return src, ""


# ── 编排入口 + 配置 + 观测 ───────────────────────────────────────────────────

_STATS: Dict[str, int] = {"monologue": 0, "lang_mix_hard": 0, "lang_mix_soft": 0,
                          "unfounded_recall": 0, "recall_grounding": 0,
                          "shared_past": 0, "apology_dedup": 0,
                          "goal_meta": 0, "degenerate": 0,
                          "sendpoint_hard": 0, "sendpoint_kept": 0,
                          "sendpoint_soft": 0}


# ── #152 F1：LLM 退化循环（复读机化）确定性检测 ─────────────────────────────
# 0902 23:2x 实锤（skuio 机 82VFQ6 / 截图 _1093）：CUDDLESTHECAT 会话一条出站
# 「越来越多越来越多…」重复数百遍直达客户。既有「复读守卫」比的是**与历史出站**
# 的相似度（防把 4 天前说过的话原样再说），对**单条内部**的 token 循环视而不见——
# 这就是漏网口。退化是纯形态问题，不需要 LLM 判断：同一个 n-gram 在文本里
# **连续**出现 ≥ 阈值次即判退化，截到第一次出现处；截空（整条都是循环）交调用方
# 按「不发+进待处理」处理。n-gram 用字符级（1..8 字）——中文无词边界，
# 「越来越多」= 4 字 gram 连续重复；英文 "very very very" 也能靠 "very " 5 字 gram 命中。
_DEGEN_MIN_REPEAT = 5          # 连续重复次数阈值（正常口语强调最多 2-3 次）
_DEGEN_MAX_GRAM = 12           # 最长循环单元（字符）
_DEGEN_MIN_SPAN = 20           # 循环总跨度下限（字符），短循环如「哈哈哈哈哈」不算


def detect_degenerate_loop(
    text: str, *, min_repeat: int = _DEGEN_MIN_REPEAT,
) -> Optional[Tuple[int, int, str]]:
    """找文本里首个退化循环 → ``(start, end, unit)``；无 → None。

    对每个起点 i、每个单元长 g（1..MAX_GRAM），数 ``text[i:i+g]`` 从 i 起连续重复的
    次数；次数 ≥ min_repeat 且总跨度 ≥ MIN_SPAN 即命中。O(n·G·k) 但 n 为单条
    出站文本（≤ 数千字），实测微秒级。
    """
    s = text or ""
    n = len(s)
    if n < _DEGEN_MIN_SPAN:
        return None
    for i in range(n):
        for g in range(1, _DEGEN_MAX_GRAM + 1):
            if i + g * min_repeat > n:
                break
            unit = s[i:i + g]
            if not unit.strip():
                continue
            k = 1
            while s[i + k * g:i + (k + 1) * g] == unit:
                k += 1
            if k >= min_repeat and k * g >= _DEGEN_MIN_SPAN:
                return i, i + k * g, unit
    return None


def strip_degenerate_loop(text: str) -> Tuple[str, Optional[str]]:
    """截断退化循环：保留循环前的正常开头 + 单元**一次**，丢掉其后全部重复及尾随
    内容（尾随内容在退化之后生成，同属不可信）。循环从文本开头就开始（没有任何
    正常前文）→ 整条视为退化，返回空串——「越来越多」四个字单独发给客户同样是
    废话，不值得保留。返回 ``(清理后文本, 命中的单元)``；未命中原样。"""
    hit = detect_degenerate_loop(text)
    if not hit:
        return text, None
    start, _end, unit = hit
    head = text[:start]
    if not head.strip():
        return "", unit
    cleaned = (head + unit).rstrip(" ，,、")
    return cleaned, unit


def guard_stats() -> Dict[str, int]:
    return dict(_STATS)


def resolve_cfg(config: Optional[Dict[str, Any]]) -> Dict[str, bool]:
    """读 ``companion.outbound_text_guard``；暴露风险守卫族默认开。

    ``vocative``/``lang_pin``（实施91 #105/#106）＝收口点呼格纠正与铆定语言
    兜底的子开关（消费方在 ``sendpoint_guard``，与本模块共用同一配置段——
    收口点守卫是一个家族，开关不散落两处）。
    """
    raw: Dict[str, Any] = {}
    try:
        raw = (((config or {}).get("companion") or {})
               .get("outbound_text_guard") or {})
        if not isinstance(raw, dict):
            raw = {}
    except Exception:
        raw = {}
    return {
        "enabled": bool(raw.get("enabled", True)),
        "monologue": bool(raw.get("monologue", True)),
        "lang_mix": bool(raw.get("lang_mix", True)),
        "unfounded_recall": bool(raw.get("unfounded_recall", True)),
        "recall_grounding": bool(raw.get("recall_grounding", True)),
        "apology_dedup": bool(raw.get("apology_dedup", True)),
        "vocative": bool(raw.get("vocative", True)),
        "lang_pin": bool(raw.get("lang_pin", True)),
        "shared_past": bool(raw.get("shared_past", True)),
        # #109 第4病：目标话术渗味（"that's the first step" 类步骤感元话术）
        "goal_meta": bool(raw.get("goal_meta", True)),
        # #152 F1：LLM 退化循环（同 n-gram 连续重复 ≥5）截断
        "degenerate": bool(raw.get("degenerate", True)),
    }


def apply_outbound_text_guard(
    text: str, cfg: Optional[Dict[str, bool]] = None, *,
    user_turns: Optional[int] = None, has_memory: bool = False,
    user_texts: Optional[List[str]] = None, memory_text: str = "",
    recent_assistant_texts: Optional[List[str]] = None,
    relationship_stage: str = "",
) -> Tuple[str, Dict[str, Any]]:
    """出稿口统一入口：旁白 → 无出处引用 → 回忆接地 → 共同经历 → 认错去重 → 混语。

    meta = ``{"monologue_hits": [...], "recall_hits": [...],
    "recall_grounding_hits": [...], "shared_past_hits": [...],
    "apology_hits": [...],
    "lang_mix": ""|"soft"|"hard_stripped"|"hard_kept"}``。
    ``user_turns``/``has_memory`` 供 B104；``user_texts``/``memory_text`` 供
    #91-A/#110 接地锁；``recent_assistant_texts`` 供 #91-C 口癖去重；
    ``relationship_stage`` 供 #110 关系阶段闸（各自缺料时对应守卫整体
    不动手）。任何内部异常返回原文（调用方兜 try 双保险）。
    """
    meta: Dict[str, Any] = {"monologue_hits": [], "recall_hits": [],
                            "recall_grounding_hits": [],
                            "shared_past_hits": [], "apology_hits": [],
                            "lang_mix": ""}
    src = text or ""
    if not src.strip():
        return src, meta
    c = cfg or {"enabled": True, "monologue": True, "lang_mix": True,
                "unfounded_recall": True, "recall_grounding": True,
                "apology_dedup": True, "shared_past": True}
    if not c.get("enabled", True):
        return src, meta
    out = src
    # #152 F1：退化循环放第一道——后面的守卫对「越来越多×300」毫无意义，且截断
    # 后的短文本才是它们该看的东西。截空＝整条都是循环，meta 标 degenerate_empty
    # 让调用方按「不发+进待处理」处理，绝不把退化稿发给客户。
    if c.get("degenerate", True):
        out, unit = strip_degenerate_loop(out)
        if unit is not None:
            meta["degenerate_unit"] = unit
            _STATS["degenerate"] += 1
            if not out.strip():
                meta["degenerate_empty"] = True
                return "", meta
    if c.get("monologue", True):
        out, hits = sanitize_inner_monologue(out)
        if hits:
            meta["monologue_hits"] = hits
            _STATS["monologue"] += 1
    if c.get("unfounded_recall", True):
        out, rhits = strip_unfounded_recall(
            out, user_turns=user_turns, has_memory=has_memory)
        if rhits:
            meta["recall_hits"] = rhits
            _STATS["unfounded_recall"] += 1
    # #91-A：回忆断言接地（B104 之后——首答一刀切已把零轮场景清掉，这里
    # 管有历史但断言内容对不上的现编）。
    if c.get("recall_grounding", True):
        out, ghits = strip_hallucinated_recall(
            out, user_texts=user_texts, memory_text=memory_text)
        if ghits:
            meta["recall_grounding_hits"] = ghits
            _STATS["recall_grounding"] += 1
    # #110：共同经历叙事接地锁（#91-A 的语法盲区——「我们一起…」叙事式虚构
    # 不带引用句形；初识/试探期直接禁，深阶段走接地）。
    if c.get("shared_past", True):
        out, shits = strip_ungrounded_shared_past(
            out, user_texts=user_texts, memory_text=memory_text,
            relationship_stage=relationship_stage)
        if shits:
            meta["shared_past_hits"] = shits
            _STATS["shared_past"] += 1
    # #91-C：认错口癖同会话去重（换变体不裸剥，句子结构恒完整）。
    if c.get("apology_dedup", True):
        out, ahits = dedup_apology_catchphrase(out, recent_assistant_texts)
        if ahits:
            meta["apology_hits"] = ahits
            _STATS["apology_dedup"] += 1
    # #109 第4病：目标话术渗味——步骤感元话术（"that's the first step"）绝不
    # 对客户出现；子段级剥除保住同句正常内容。
    if c.get("goal_meta", True):
        out, ghits2 = strip_goal_meta_talk(out)
        if ghits2:
            meta["goal_meta_hits"] = ghits2
            _STATS["goal_meta"] += 1
    if c.get("lang_mix", True):
        verdict = detect_lang_mix(out)
        if verdict["action"] == "hard":
            stripped = strip_minority_script(out)
            if stripped != out:
                out = stripped
                meta["lang_mix"] = "hard_stripped"
            else:
                meta["lang_mix"] = "hard_kept"
            _STATS["lang_mix_hard"] += 1
        elif verdict["action"] == "soft":
            meta["lang_mix"] = "soft"
            _STATS["lang_mix_soft"] += 1
    if not (out or "").strip():
        return src, meta
    return out, meta
