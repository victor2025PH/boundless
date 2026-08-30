"""人设一致性守卫（陪聊沉浸感保护）。

LLM 不总是遵守 prompt 里"禁止使用 X"的指令；一旦回复漏出客服腔
（"有什么可以帮您的"）或自曝 AI 身份（"作为一个人工智能"），情感陪聊的"真人感"
就瞬间崩塌——这是本产品最致命的体验事故。本模块在回复生成后做一次**确定性**后置体检：

- 命中人设 ``speaking.forbidden_phrases``；
- 若 ``identity.deny_ai`` 为真，命中"自曝 AI 身份"的模式（保守匹配，避免误伤否定句）。

命中则**按句剥离**违规句子（保留其余内容），绝不返回空串
（极端情况整段都违规则回退原文 + 由调用方记日志/指标）。

纯函数、平台无关、可单测。真正的"违规重写（重新生成）"留作上层可选优化。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

# 自曝 AI 身份的模式（仅 deny_ai 人设启用）。保守匹配："我是…AI" 命中，
# 但 "我不是 AI" 不命中（否定句不算露馅）。
_AI_SELF_ID_PATTERNS = [
    re.compile(r"作为(一个|一名)?\s*(AI|A\.?I\.?|人工智能|语言模型|大模型|聊天机器人|机器人|智能助手|虚拟助手)", re.I),
    re.compile(r"我(是|就是)(一个|一名|个|你的)?\s*(AI|A\.?I\.?|人工智能|语言模型|大模型|聊天机器人|机器人|智能助手|虚拟助手)", re.I),
    re.compile(r"(身为|作为)[^。！？!?\n]{0,8}(语言模型|人工智能|大模型)", re.I),
    re.compile(r"\bas an? (ai|artificial intelligence|language model)\b", re.I),
    re.compile(r"\bi[' ]?a?m an? (ai|artificial intelligence|language model)\b", re.I),
    re.compile(r"\blanguage model\b", re.I),
]

# 感知语境豁免（2026-07-20 阿龙实测误报）：「我怕你觉得我是AI客服机器人」是
# 「怕被当成机器人」的拟人打趣（强化真人感），不是身份自曝——与既有「否定句不算
# 露馅」同一类。命中片段若紧跟在感知动词后（觉得/以为/当成…，允许 ≤4 个非标点
# 字符间隔，如「觉得其实我是AI」）→ 不算违规。刻意不收「说」（"老实说我是AI"
# 是真自曝），宁可漏豁免不可漏拦截。
_PERCEPTION_PREFIX_RE = re.compile(
    r"(觉得|以为|当成|当作|误会|怀疑)[^。！？!?\n]{0,4}$"
)

# ── 已撤销旧设定的「认领」检测（2026-08-04 P2 期，出站兜底）────────────────────
# prompt 钉子（boundaries.retired_facts）偶尔还是会被历史窗口压过——AI 又说出
# 「我家猫今天很乖」。本检测**刻意只抓第一人称认领**：
#   命中 = 第一人称归属/偏好标记 + ≤8 个非标点字符间隔 + 锚词；
#   豁免 = 窗口内含否定（「我没有养猫」是钉子要求的正确澄清，绝不能剥）。
# 「你家的猫真可爱」（聊客户的猫）没有第一人称标记 → 不命中——撤销语义本就允许
# 正常参与话题，只禁认领。宁可漏拦（钉子已是主防线）不可误伤正常社交。
_RETIRED_FP_MARKER = r"(我们家|我家|我的|我养|我新养|我喜欢|我超喜欢|我最爱|我爱|\bmy\b|\bour\b)"
_RETIRED_NEG_RE = re.compile(r"(没有|没在|没养|从没|从来没|不再|不养|哪有|不是|别提)")


def _retired_claim_re(term: str) -> "re.Pattern":
    return re.compile(
        _RETIRED_FP_MARKER + r"[^。！？!?\n]{0,8}" + re.escape(term), re.I)


def _matches_retired_claims(text: str, terms: List[str]) -> List[str]:
    out: List[str] = []
    s = str(text or "")
    for term in terms or []:
        t = str(term).strip()
        if not t:
            continue
        for m in _retired_claim_re(t).finditer(s):
            window = s[max(0, m.start() - 6):m.end()]
            if _RETIRED_NEG_RE.search(window):
                continue          # 否定澄清是钉子要求的正确行为
            out.append(m.group(0))
            break                 # 每词至多记一个片段（与既有模式一致）
    return out

# 按中英文句末标点切句（保留标点，便于无缝重组剩余句子）
_SENTENCE_SPLIT_RE = re.compile(r"[^。！？!?\n]*[。！？!?\n]|[^。！？!?\n]+")

# 无句末标点的中文口语流用空格当子句边界（拟人人设常用风格：「行 那再给你发一条
# 你听听」）。子句 = 非空白串 + 其尾随空白（保留空白，剔除违规子句后无缝重组）。
_WS_CLAUSE_RE = re.compile(r"\S+\s*")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_SENT_PUNCT_RE = re.compile(r"[。！？!?\n]")


def collect_forbidden(
    persona: Dict[str, Any], *, honest_identity: bool = False
) -> Dict[str, Any]:
    """从人设 dict 抽取守卫所需的禁用项。

    ``retired_terms``（2026-08-04）＝撤销旧设定的锚词（仅结构化条目提供，
    legacy 纯文案条目无锚词=不参与守卫）；异常一律空列表，绝不拖垮守卫。

    ``honest_identity``（WP-4 合规模式，2026-08-17）＝``compliance.disclosure.
    honest_identity`` 开时由调用方传 True：(a) ``deny_ai`` 按 False 处理——
    「自曝 AI 身份」不再算违规（客户直问「你是 AI 吗」的如实回答不得被剥）；
    (b) ``forbidden_phrases`` 里**身份类**条目（「作为AI」「我是语言模型」这类
    会剥掉诚实承认的短语）一并豁免——判类复用 ``_AI_SELF_ID_PATTERNS`` 单一
    口径，防第二套正则漂移；非身份类禁语（客服腔等）照常生效。默认 False =
    行为与旧版逐字节一致。
    """
    speaking = (persona or {}).get("speaking") or {}
    identity = (persona or {}).get("identity") or {}
    phrases = [
        str(p).strip()
        for p in (speaking.get("forbidden_phrases") or [])
        if str(p).strip()
    ]
    if honest_identity:
        phrases = [p for p in phrases if not _matches_ai_self_id(p)]
    try:
        from src.utils.persona_retired import retired_guard_terms
        retired = retired_guard_terms(persona)
    except Exception:
        retired = []
    deny_ai = bool(identity.get("deny_ai")) and not honest_identity
    return {"phrases": phrases, "deny_ai": deny_ai,
            "retired_terms": retired}


def _norm(s: str) -> str:
    """归一化用于子串比对：去所有空白 + 小写（中文不受影响，英文大小写/空格鲁棒）。"""
    return re.sub(r"\s+", "", s or "").lower()


def _matches_phrase(haystack_norm: str, phrases: List[str]) -> List[str]:
    out: List[str] = []
    for p in phrases:
        np = _norm(p)
        if np and np in haystack_norm:
            out.append(p)
    return out


def _matches_ai_self_id(text: str) -> List[str]:
    out: List[str] = []
    for pat in _AI_SELF_ID_PATTERNS:
        for m in pat.finditer(text):
            # 感知语境豁免：「(你)觉得/以为/当成…我是AI」不是自曝（见常量注释）
            if _PERCEPTION_PREFIX_RE.search(text[:m.start()]):
                continue
            out.append(m.group(0))
            break  # 每个模式至多记一个片段（与旧行为一致）
    return out


def matches_ai_self_identity(text: str) -> List[str]:
    """公共入口：文本中「自曝 AI 身份」的命中片段（含否定句/感知语境豁免）。

    供 quality_tracker 等监控组件复用同一判定口径，避免两套正则漂移
    （此前 quality_tracker 自带简版正则，把「怕你觉得我是AI机器人」误报成 identity_leak）。
    """
    return _matches_ai_self_id(str(text or ""))


def find_violations(
    text: str, persona: Dict[str, Any], *, honest_identity: bool = False
) -> List[str]:
    """返回 ``text`` 中命中的违规片段清单（空 = 合规）。"""
    if not text:
        return []
    fb = collect_forbidden(persona, honest_identity=honest_identity)
    hits = _matches_phrase(_norm(text), fb["phrases"])
    if fb["deny_ai"]:
        hits.extend(_matches_ai_self_id(text))
    if fb.get("retired_terms"):
        hits.extend(_matches_retired_claims(text, fb["retired_terms"]))
    return hits


def _split_sentences(text: str) -> List[str]:
    parts = [m.group(0) for m in _SENTENCE_SPLIT_RE.finditer(text) if m.group(0)]
    # 整段无句末标点的中文口语流（空格代逗号句号的人设风格）→ 按空格切子句。
    # 否则整段=一个"句子"：一处违规 → 全删 → 触发「删光回退原文」= 守卫形同虚设
    # （2026-07-20 阿龙实测：日志喊「已剥离」实际原样发出的根因）。
    if (len(parts) <= 1 and text and not _SENT_PUNCT_RE.search(text)
            and _CJK_RE.search(text) and re.search(r"\s", text.strip())):
        return [m.group(0) for m in _WS_CLAUSE_RE.finditer(text)]
    return parts


def _sentence_violates(sentence: str, fb: Dict[str, Any]) -> bool:
    if _matches_phrase(_norm(sentence), fb["phrases"]):
        return True
    if fb["deny_ai"] and _matches_ai_self_id(sentence):
        return True
    if fb.get("retired_terms") and _matches_retired_claims(
            sentence, fb["retired_terms"]):
        return True
    return False


def sanitize(
    text: str, persona: Dict[str, Any], *, honest_identity: bool = False
) -> Tuple[str, List[str]]:
    """剥离违规句，返回 ``(清洁文本, 命中清单)``。

    - 无禁用项或无命中 → 原样返回（命中清单为空）。
    - 有命中 → 删掉含违规片段的整句，保留其余；
    - 若删光（整段都违规）→ 先尝试 inline 抹掉禁用短语；仍空则回退原文（绝不返回空）。
    - ``honest_identity=True``（WP-4 合规模式）→ 身份类检测整体豁免，其余照常
      （语义见 :func:`collect_forbidden`）。
    """
    if not text:
        return text, []
    fb = collect_forbidden(persona, honest_identity=honest_identity)
    if not fb["phrases"] and not fb["deny_ai"] and not fb.get("retired_terms"):
        return text, []
    violations = find_violations(text, persona, honest_identity=honest_identity)
    if not violations:
        return text, []
    kept = [s for s in _split_sentences(text) if not _sentence_violates(s, fb)]
    cleaned = "".join(kept).strip()
    if not cleaned:
        cleaned = text
        for p in fb["phrases"]:
            if p:
                cleaned = re.sub(re.escape(p), "", cleaned, flags=re.I)
        cleaned = cleaned.strip()
        # 子句级降级（2026-08-04 真机实测缺口）：单句回复（只有逗号）里
        # 「刚下课～我家猫特别黏人，你吃了吗？」——句级剥离把整段删光 →
        # 回退原文＝认领句原样出站。逗号级再切一刀只丢违规子句，其余保留。
        if cleaned == text.strip() or not cleaned:
            base = cleaned or text
            clauses = [m.group(0) for m in
                       re.finditer(r"[^，,;；]+[，,;；]?", base)]
            kept2 = [c for c in clauses if not _sentence_violates(c, fb)]
            c2 = "".join(kept2).strip("，,;； ")
            if c2 and c2 != base.strip():
                cleaned = c2
        if not cleaned:
            return text, violations
    return cleaned, violations


# ── 错误自称名守卫（2026-08-08「David Lin」事故）────────────────────────────────
# 实录：客户（显示名 David）质疑「Look like ai」，AI 回「I'm just David Lin, a real
# guy…」——把**对方的名字**和自己的姓氏缝成新身份自称，3 分钟后被问「Who are you?」
# 又改口真名 Lin Xiaoyu，当场穿帮。prompt 侧「自称规则·强制」在场仍失守 ⇒ 需要出站
# 硬护栏。判罚分级（宁可漏拦不误拦，与本模块其余检测同哲学）：
#   hard（剥除）＝ ① 自称名含**对方名字** token（借名缝合，任何脚本）；
#                 ② CJK 强模式（我叫/我的名字是/叫我）报出非白名单 CJK 名；
#                 ③ 拉丁 ≥2 词全名且白名单已含拉丁变体（names.* 西名或拼音罗马化
#                    在册）仍不匹配——白名单不完备时绝不启用此档。
#   soft（只记日志）＝ 其余非白名单自称（如单词英文名/绰号玩笑），先观测后收紧。
# 白名单 = persona.name + names.*（full_western/english/german/french/nickname）
#          + 调用方补充（spoken_name/ai_name 覆写）+ CJK 条目的拼音变体
#          （pypinyin 软依赖，缺库自动少一层，判罚档 ③ 随之自动收窄）。
# 引导词大小写不敏感（(?i: ) 作用域旗标），但**名字捕获组保持大小写敏感**——
# 「首字母大写」正是英文里区分 "I'm fine" 与 "I'm David" 的关键信号。
_SELF_NAME_EN_RE = re.compile(
    r"\b(?i:i\s*['’]?\s*m|i\s+am|my\s+name(?:['’]s|\s+is)|call\s+me)\s+"
    r"(?:(?i:just|actually|really|still|now|officially)\s+)*"
    r"([A-Z][A-Za-z'’\-]*(?:\s+[A-Z][A-Za-z'’\-]*){0,2})"
)
_SELF_NAME_CJK_STRONG_RE = re.compile(
    r"(?<![不没别可])(?:我叫|我的名字[是叫]|叫我)\s*([\u4e00-\u9fff][\u4e00-\u9fff·]{0,5})"
)
_SELF_NAME_CJK_WEAK_RE = re.compile(
    r"(?<![不没别])(?:我是|我就是)\s*([\u4e00-\u9fff][\u4e00-\u9fff·]{1,5})"
)
# 英文感知/转述前缀（"you think I'm David" 不是自称）；CJK 侧复用 _PERCEPTION_PREFIX_RE
_EN_PERCEPTION_PREFIX_RE = re.compile(
    r"(?:think|thought|assumed?|guess(?:ed)?|wish|pretend(?:ed)?)\s*$", re.I)
# 英文名 token 停用词：命中任一 token ＝ 不是名字（"I'm Just Kidding" / "I'm So Sorry"）
_EN_NAME_STOPWORDS = frozenset({
    "just", "kidding", "sorry", "fine", "good", "okay", "ok", "sure",
    "serious", "done", "back", "here", "home", "not", "so", "really",
    "busy", "tired", "late", "happy", "sad", "glad", "ready", "right",
    "wrong", "sick", "free", "online", "offline", "alright", "great",
})
_CJK_TRAIL_PARTICLES = "吧哦呀啦哈嘛呢哟喔噢咯呗啊呐嘞哩喽欸诶嗯～~！!。，,"
_LATIN_TOKEN_RE = re.compile(r"[A-Za-z]{2,}")


def _pinyin_variants(name: str) -> List[str]:
    """CJK 名 → 拼音罗马化变体（全名连写 / 名不带姓）。pypinyin 缺失返回空。"""
    s = str(name or "").strip()
    if not s or not _CJK_RE.search(s):
        return []
    try:
        from pypinyin import lazy_pinyin
        syls = [str(x).strip().lower() for x in lazy_pinyin(s) if str(x).strip()]
    except Exception:
        return []
    if not syls:
        return []
    out = {"".join(syls)}
    if len(syls) >= 2:
        out.add("".join(syls[1:]))          # 名（去姓）：xiaoyu
    return [v for v in out if len(v) >= 2]


def build_self_name_allowlist(
    persona: Dict[str, Any], extra_names: List[str] | None = None
) -> List[str]:
    """收集「本人设可以用来自称」的全部名字（含 names.* 西名与拼音变体）。绝不抛。"""
    out: List[str] = []
    try:
        p = persona or {}
        cands: List[str] = [str(p.get("name") or "")]
        names = p.get("names") or {}
        if isinstance(names, dict):
            for k in ("full_western", "english", "german", "french", "nickname"):
                cands.append(str(names.get(k) or ""))
        for x in (extra_names or []):
            cands.append(str(x or ""))
        seen = set()
        for c in cands:
            c = c.strip()
            if not c:
                continue
            variants = [c] + _pinyin_variants(c)
            # CJK 姓名补「去姓的名」（林小语 → 小语；复姓 4 字 → 再补后 2 字）：
            # 「叫我小语」是最常见的合法自称形态，白名单没有它=strong 档必误伤。
            if _CJK_RE.search(c) and len(c) >= 3:
                variants.append(c[1:])
                if len(c) >= 4:
                    variants.append(c[2:])
            for v in variants:
                nv = _norm(v)
                if len(nv) >= 2 and nv not in seen:
                    seen.add(nv)
                    out.append(v)
    except Exception:
        pass
    return out


def _peer_norm_tokens(peer_names: List[str] | None) -> Tuple[List[str], List[str]]:
    """对方名 → (拉丁 token 集, CJK 归一串集)。@username / first+last 都拆词。"""
    latin: List[str] = []
    cjk: List[str] = []
    for p in (peer_names or []):
        s = str(p or "").strip().lstrip("@")
        if not s:
            continue
        for t in _LATIN_TOKEN_RE.findall(s):
            tl = t.lower()
            if tl not in _EN_NAME_STOPWORDS and tl not in latin:
                latin.append(tl)
        cs = "".join(ch for ch in s if _CJK_RE.match(ch))
        if len(cs) >= 2 and cs not in cjk:
            cjk.append(cs)
    return latin, cjk


def _expand_allowed_norms(allowed_names: List[str] | None) -> List[str]:
    """归一化白名单 + CJK 姓名自动补「去姓的名」变体（检测器内置，
    调用方传裸名单也不丢保护：「叫我小语」对白名单只有「林小语」的部署零误伤）。"""
    out: List[str] = []
    seen = set()
    for a in (allowed_names or []):
        s = str(a or "").strip()
        if not s:
            continue
        cands = [s]
        if _CJK_RE.search(s) and len(s) >= 3:
            cands.append(s[1:])
            if len(s) >= 4:
                cands.append(s[2:])
        for c in cands:
            n = _norm(c)
            if len(n) >= 2 and n not in seen:
                seen.add(n)
                out.append(n)
    return out


def _claim_allowed(claim_norm: str, allowed_norms: List[str]) -> bool:
    if not claim_norm or len(claim_norm) < 2:
        return True          # 太短不判（保守放行）
    for a in allowed_norms:
        if len(a) >= 2 and (claim_norm == a or claim_norm in a or a in claim_norm):
            return True
    return False


def _iter_self_name_claims(text: str):
    """产出 (claim_text, is_strong, is_latin)。已剔除否定/感知/所有格/停用词形态。"""
    s = str(text or "")
    if not s:
        return
    for m in _SELF_NAME_EN_RE.finditer(s):
        prefix = s[:m.start()]
        if _EN_PERCEPTION_PREFIX_RE.search(prefix[-24:]):
            continue
        raw = m.group(1).strip()
        toks = [t for t in re.split(r"\s+", raw) if t]
        # 所有格截断（"David's friend" 不是自称）：遇 's 丢弃该词及其后
        kept: List[str] = []
        for t in toks:
            if re.search(r"['’]s$", t):
                break
            kept.append(t)
        if not kept:
            continue
        if any(t.lower() in _EN_NAME_STOPWORDS for t in kept):
            continue
        yield " ".join(kept), True, True
    for pat, strong in ((_SELF_NAME_CJK_STRONG_RE, True),
                        (_SELF_NAME_CJK_WEAK_RE, False)):
        for m in pat.finditer(s):
            if _PERCEPTION_PREFIX_RE.search(s[:m.start()]):
                continue
            raw = m.group(1).strip().rstrip(_CJK_TRAIL_PARTICLES)
            if "的" in raw:      # 「我是大卫的朋友」＝关系描述不是自称
                continue
            if len(raw) < 2:
                continue
            yield raw, strong, False


def find_wrong_self_name(
    text: str,
    allowed_names: List[str] | None,
    peer_names: List[str] | None = None,
) -> Tuple[List[str], List[str]]:
    """返回 ``(hard_hits, soft_suspicions)``——错误自称名的分级命中。

    hard = 高置信必错（借对方名 / CJK 强模式报非白名单名 / 拉丁全名且白名单
    已含拉丁变体仍不匹配）→ 调用方应剥除；soft = 非白名单但置信不足 → 只观测。
    纯函数，绝不抛。
    """
    hard: List[str] = []
    soft: List[str] = []
    try:
        allowed_norms = _expand_allowed_norms(allowed_names)
        peer_latin, peer_cjk = _peer_norm_tokens(peer_names)
        allow_has_latin = any(re.search(r"[a-z]", a) for a in allowed_norms)
        for claim, strong, is_latin in _iter_self_name_claims(text):
            cn = _norm(claim)
            if _claim_allowed(cn, allowed_norms):
                continue
            borrowed = False
            if is_latin:
                ctoks = {t.lower() for t in re.split(r"\s+", claim) if t}
                borrowed = bool(ctoks & set(peer_latin))
            else:
                borrowed = any(pc in cn or cn in pc for pc in peer_cjk)
            if borrowed:
                hard.append(claim)
            elif strong and not is_latin and allowed_norms:
                hard.append(claim)          # 我叫〈非白名单 CJK 名〉
            elif (is_latin and len(claim.split()) >= 2
                    and allowed_norms and allow_has_latin):
                hard.append(claim)          # 拉丁全名 + 白名单有拉丁变体仍不匹配
            else:
                soft.append(claim)
    except Exception:
        return [], []
    return hard, soft


# 英文感知句界（仅自称名守卫用）：既有 _split_sentences 不认 ASCII 句点——
# 英文回复 "…sweet. What makes…?" 会整段并成一句，一处违规连带剥掉好句。
# 此处补「.!? 后跟空白」为句界（防 3.5 / example.com / U.S. 中间误切），
# 刻意不动 _split_sentences 本体（既有 sanitize 行为/测试保持原样）。
_SN_SENT_BOUNDARY_RE = re.compile(r"(?:(?<=[。！？!?\n])|(?<=[.!?])(?=\s))")


def _split_sentences_sn(text: str) -> List[str]:
    s = str(text or "")
    if not s:
        return []
    cuts = sorted({m.start() for m in _SN_SENT_BOUNDARY_RE.finditer(s)
                   if 0 < m.start() < len(s)})
    frags: List[str] = []
    prev = 0
    for i in cuts:
        frags.append(s[prev:i])
        prev = i
    frags.append(s[prev:])
    return [f for f in frags if f]


def _hit_is_borrowed(claim: str, peer_names: List[str] | None) -> bool:
    """hard 命中是否属「借对方名」档（与 find_wrong_self_name 内部同判据）。

    borrowed＝身份级穿帮（拿客户的名字自称），残剥也比发出去强；
    非 borrowed 的 hard（白名单外自称）实证主因是**白名单不完备**（自建人设
    名没进白名单，B74 `_352`），残剥反而必穿帮。
    """
    try:
        cn = _norm(str(claim or ""))
        if not cn:
            return False
        peer_latin, peer_cjk = _peer_norm_tokens(peer_names)
        if re.search(r"[a-z]", cn):
            ctoks = {t.lower() for t in re.split(r"\s+", str(claim)) if t}
            return bool(ctoks & set(peer_latin))
        return any(pc in cn or cn in pc for pc in peer_cjk)
    except Exception:
        return False


def sanitize_self_name(
    text: str,
    allowed_names: List[str] | None,
    peer_names: List[str] | None = None,
) -> Tuple[str, List[str], List[str]]:
    """剥离 hard 级错误自称句，返回 ``(清洁文本, hard 命中, soft 观测)``。

    与 :func:`sanitize` 同一套「按句剥离 → 剥光则 inline 抹名 → 绝不返回空」策略。

    B74 修正（实施67，`_352` 实录「叫我。朋友都这么喊我。」）：hard 自称名必然
    处于自介引导语境（我叫/叫我/call me…——检测正则本身就锚定这些引导词），
    inline 抹名会把自介句剁成「叫我。」残句＝100% 穿帮，比误放行更糟。故 inline
    抹名兜底**只对 borrowed（借对方名）档执行**（David Lin 事故金标不回退——
    借名缝合发出去是身份级事故，残句是两害相权）；非 borrowed 的 hard 剥光时
    保留原文（宁可漏拦不误伤，白名单不完备时这正是人设真名在自报家门）。
    """
    t = str(text or "")
    if not t:
        return t, [], []
    hard, soft = find_wrong_self_name(t, allowed_names, peer_names)
    if not hard:
        return t, hard, soft
    kept = [
        s for s in _split_sentences_sn(t)
        if not find_wrong_self_name(s, allowed_names, peer_names)[0]
    ]
    cleaned = "".join(kept).strip()
    if not cleaned:
        borrowed = [h for h in hard if _hit_is_borrowed(h, peer_names)]
        if not borrowed:
            return t, hard, soft          # 非借名 → 残剥必穿帮，保留原文
        cleaned = t
        for h in borrowed:
            cleaned = re.sub(re.escape(h), "", cleaned)
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
        if not cleaned or _norm(cleaned) == _norm(t):
            return t, hard, soft          # 无法安全剥离 → 保留原文（调用方记日志）
    return cleaned, hard, soft


# ── 称呼混淆守卫（B42 2026-08-22）：用**自己的**人设名呼叫对方 ────────────────
#
# 实录（1.0.46 _236）：对客户 Nicks 说 "you're not that old, Steven"——Steven 是
# AI 自己的人设名。上面的 find_wrong_self_name 守的是反方向（拿别人的名字自称）；
# 这里守正方向：把自己的名字砸在客户头上（呼格）。
#
# 只抓高置信**呼格形态**（句首「Name, …」/ 句尾「…, Name」），并且：
#   - 对方已知名字含该名 → 全跳（客户真叫这个名，禁了就误伤）；
#   - 名字前是自我介绍语境（我是/我叫/叫我/I'm/call me/this is…）→ 不算呼格；
#   - 对方名字**未知**（peer_names 空）→ 整体不判——没有客户资料就无法排除
#     「对方恰好同名」，宁可漏报不误伤（与孤儿引用门禁同哲学）。
# 剥离粒度＝**名字 token 本身**而非整句——"you're not that old, Steven" 去掉
# 呼格名后句子本体仍是有效回复；整句剥反而把内容杀掉。

_VOC_SELF_INTRO_TAIL_RE = re.compile(
    r"(?:我(?:就)?[是叫]|叫我|人家(?:是|叫)|这(?:里|边)是|我系|"
    r"i\s*(?:'?a?m)|it\s*'?s|this\s+is|call\s+me|name\s*(?:'?s|is)|named|-|—)"
    r"\s*[,，]?\s*$",   # 「name is, Steven」怪写法的逗号也算自介语境
    re.IGNORECASE)


def _voc_patterns(name: str) -> List[re.Pattern]:
    n = re.escape(str(name or "").strip())
    if not n:
        return []
    return [
        # 句尾呼格：…, Name / …，Name（后只许终止标点/空白）
        re.compile(r"[,，]\s*(" + n + r")\s*(?=[.!?。！？~～…\s]*$)", re.IGNORECASE),
        # 句首呼格：Name, … / Name，…
        re.compile(r"^\s*(" + n + r")\s*[,，]", re.IGNORECASE),
    ]


def find_vocative_self_name(
    text: str,
    self_names: List[str] | None,
    peer_names: List[str] | None = None,
) -> List[str]:
    """返回被用来**称呼对方**的自己人设名命中列表（高置信呼格形态）。纯函数绝不抛。"""
    hits: List[str] = []
    try:
        t = str(text or "")
        peers = [str(p or "").strip() for p in (peer_names or []) if str(p or "").strip()]
        if not t.strip() or not peers:
            return []          # 对方名未知 → 不判（无法排除同名客户）
        peer_norm = _norm(" ".join(peers))
        for name in (self_names or []):
            nm = str(name or "").strip()
            if len(nm) < 2:
                continue
            if _norm(nm) and _norm(nm) in peer_norm:
                continue       # 客户名里含此名 → 称呼合法
            for sent in _split_sentences_sn(t):
                for pat in _voc_patterns(nm):
                    m = pat.search(sent)
                    if not m:
                        continue
                    # 自我介绍语境（"…, I'm Steven" 的逗号形不落此形态，但
                    # "my name is, Steven" 类怪写法防一手）
                    if _VOC_SELF_INTRO_TAIL_RE.search(sent[:m.start(1)]):
                        continue
                    hits.append(nm)
                    break
                if nm in hits:
                    break
    except Exception:
        return []
    return hits


def strip_vocative_self_name(
    text: str,
    self_names: List[str] | None,
    peer_names: List[str] | None = None,
) -> Tuple[str, List[str]]:
    """剥离呼格位置的自己人设名（只抹名字 token，句子本体保留）。

    返回 ``(清洁文本, 命中列表)``；无命中原样返回。剥后为空回原文（绝不返回空）。
    """
    t = str(text or "")
    hits = find_vocative_self_name(t, self_names, peer_names)
    if not hits:
        return t, []
    out_sents: List[str] = []
    for sent in _split_sentences_sn(t):
        s = sent
        for nm in hits:
            for pat in _voc_patterns(nm):
                m = pat.search(s)
                if m and not _VOC_SELF_INTRO_TAIL_RE.search(s[:m.start(1)]):
                    # 连同引导逗号一起抹（句首形态抹尾随逗号）
                    s = (s[:m.start()] + s[m.end():]) if m.start() > 0 or s[:m.start()].strip() \
                        else s[m.end():]
        out_sents.append(s)
    cleaned = "".join(out_sents)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
    if not cleaned:
        return t, hits
    return cleaned, hits


# ── #24 称呼互换守卫（0830 babe/baba 实锤）────────────────────────────────────
#
# 档案明写「你叫对方 babe / 对方叫你 baba」，AI 仍被客户满屏「baba」带跑、把
# 对方叫成 baba（「good morning baba」形）。prompt 硬钉子是第一道（persona_manager
# ``_build_address_pin``），这里是出站兜底：呼格位置的 peer_calls_you → call_peer。
#
# 只动**呼格形态**（三种）：带逗号呼格（复用 _voc_patterns）、问候语+称呼收尾
# （good morning baba / 晚安 baba——事故原形没有逗号）、句首独立称呼起头
# （baba 你睡了吗）。元语句（叫我/叫你/call me…讨论称呼本身）整句跳过——
# 「你叫我 baba 的时候好可爱」是合法引用，换词反而穿帮；所有格「我是你的 baba」
# （AI 自指）不属呼格，天然不动。

_ADDR_META_RE = re.compile(
    r"叫我|喊我|叫你|喊你|你叫|别叫|不要叫|call(?:s|ing|ed)?\s+(?:me|you)|"
    r"don'?t call", re.IGNORECASE)

_GREET_WORDS = (
    r"good\s+(?:morning|night|evening|afternoon)|morning|night|"
    r"hi|hey|hello|miss\s+you|love\s+you|"
    r"早安|早呀|早|晚安|嗨|想你了|爱你")


def _addr_voc_patterns(name: str) -> List[re.Pattern]:
    n = re.escape(str(name or "").strip())
    if not n:
        return []
    return [
        # 逗号呼格（与 _voc_patterns 同形）：…, Y 结尾 / Y, … 开头
        re.compile(r"[,，]\s*(" + n + r")\s*(?=[.!?。！？~～…\s]*$)",
                   re.IGNORECASE),
        re.compile(r"^\s*(" + n + r")\s*[,，]", re.IGNORECASE),
        # 问候语 + 称呼收尾（事故原形，无逗号）：good morning Y / 晚安 Y
        re.compile(r"(?:" + _GREET_WORDS + r")\s*[,，]?\s+(" + n + r")\b"
                   r"(?=[.!?。！？~～…\s]*$)", re.IGNORECASE),
        # 句首独立称呼起头：Y 你睡了吗 / Y what are you doing
        re.compile(r"^\s*(" + n + r")(?=\s+\S|[，,]\s*\S)", re.IGNORECASE),
    ]


def swap_vocative_peer_call(
    text: str, call_peer: str, peer_calls_you: str,
) -> Tuple[str, List[str]]:
    """呼格位置的「对方叫你的称呼」→「你叫对方的称呼」（#24 出站兜底）。

    返回 ``(纠正后文本, 命中列表)``；任一字段为空/两者相同/无命中原样返回。
    纯函数绝不抛；只替换称呼 token，句子本体不动。
    """
    t = str(text or "")
    cp = str(call_peer or "").strip()
    py = str(peer_calls_you or "").strip()
    if not t.strip() or not cp or not py or _norm(cp) == _norm(py):
        return t, []
    hits: List[str] = []
    out_sents: List[str] = []
    try:
        pats = _addr_voc_patterns(py)
        for sent in _split_sentences_sn(t):
            s = sent
            if _ADDR_META_RE.search(s):
                out_sents.append(s)     # 讨论称呼本身的句子整句放行
                continue
            for pat in pats:
                m = pat.search(s)
                if not m:
                    continue
                if _VOC_SELF_INTRO_TAIL_RE.search(s[:m.start(1)]):
                    continue            # 自介语境不是呼格
                hits.append(m.group(1))
                s = s[:m.start(1)] + cp + s[m.end(1):]
            out_sents.append(s)
    except Exception:
        return t, []
    if not hits:
        return t, []
    cleaned = "".join(out_sents)
    return (cleaned if cleaned.strip() else t), hits


__all__ = [
    "collect_forbidden", "find_violations", "matches_ai_self_identity", "sanitize",
    "build_self_name_allowlist", "find_wrong_self_name", "sanitize_self_name",
    "find_vocative_self_name", "strip_vocative_self_name",
    "swap_vocative_peer_call",
]
