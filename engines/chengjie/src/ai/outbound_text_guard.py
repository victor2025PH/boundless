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
_NARRATION_ZH = (
    "叹气", "叹了", "轻叹", "微笑", "苦笑", "傻笑", "偷笑", "大笑", "轻笑",
    "沉默", "摇头", "点头", "皱眉", "挠头", "耸肩", "翻白眼", "看着", "望着",
    "盯着", "凑近", "靠近", "深呼吸", "哽咽", "抹泪", "擦泪", "撇嘴", "嘟嘴",
    "脸红", "心里", "心想", "内心", "自言自语", "小声", "低声", "停顿",
    "沉思", "想了想", "环顾", "装作", "假装", "扶额", "捂脸", "揉了揉",
)
_NARRATION_ZH_SHORT = {"笑", "哭", "叹", "汗", "尬", "无奈", "害羞", "委屈"}
_NARRATION_EN = re.compile(
    r"\b(sighs?|smiles?|smiling|laughs?|laughing|nods?|nodding|pauses?|"
    r"whispers?|whispering|blushes|blushing|shrugs?|chuckles?|grins?|"
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


# ── 编排入口 + 配置 + 观测 ───────────────────────────────────────────────────

_STATS: Dict[str, int] = {"monologue": 0, "lang_mix_hard": 0, "lang_mix_soft": 0,
                          "unfounded_recall": 0}


def guard_stats() -> Dict[str, int]:
    return dict(_STATS)


def resolve_cfg(config: Optional[Dict[str, Any]]) -> Dict[str, bool]:
    """读 ``companion.outbound_text_guard``；暴露风险守卫族默认开。"""
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
    }


def apply_outbound_text_guard(
    text: str, cfg: Optional[Dict[str, bool]] = None, *,
    user_turns: Optional[int] = None, has_memory: bool = False,
) -> Tuple[str, Dict[str, Any]]:
    """出稿口统一入口：旁白 → 无出处引用 → 混语，返回 (清洗后文本, 命中元数据)。

    meta = ``{"monologue_hits": [...], "recall_hits": [...],
    "lang_mix": ""|"soft"|"hard_stripped"|"hard_kept"}``。
    ``user_turns``/``has_memory`` 供 B104（调用方不传＝该守卫整体不动手）。
    任何内部异常返回原文（调用方兜 try 双保险）。
    """
    meta: Dict[str, Any] = {"monologue_hits": [], "recall_hits": [],
                            "lang_mix": ""}
    src = text or ""
    if not src.strip():
        return src, meta
    c = cfg or {"enabled": True, "monologue": True, "lang_mix": True,
                "unfounded_recall": True}
    if not c.get("enabled", True):
        return src, meta
    out = src
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
