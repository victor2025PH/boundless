"""入站消息 → 统一草稿引擎上下文补全（媒体 / 短消息 / 多语言切换）。

Messenger/WhatsApp RPA 在 runner 层注入 ``_peer_message_is_media`` 等字段；
Telegram 收件箱 auto-draft 此前只传纯 text，导致已开发的「像真人」规则栈
（多模态回应、短消息镜像、语言跟随）在全自动路径上形同未启用。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from src.integrations.protocol_bridge import media_placeholder
from src.skills.skill_manager import _is_meaningless_interjection_only

# ingest / Telegram 侧**裸**占位符（无描述）→ ai_client 理解的 media kind（精确匹配）
_PLACEHOLDER_KIND: Dict[str, str] = {
    "[贴纸]": "sticker",
    "[动态表情]": "gif",
    "[GIF]": "gif",
    "[图片]": "image",
    "[语音]": "voice",
    "[视频]": "video",
    "[文件]": "file",
    "[媒体]": "media",
}

# 带描述的占位前缀 → (kind, 描述=组1)。统一覆盖全平台出产格式（阶段 3 统一注入）：
#   TG A 线:   [图片内容] d / [贴纸内容] d / [表情] d（emoji demojize+贴纸 Vision）/ [视频内容] 画面…语音…
#   LINE RPA:  [图片消息] d / [LINE贴图] d / [视频消息] d / [动图消息] d / [语音消息] d / [文件消息] d
#   Messenger: [图片] d / [视频] d / [GIF] d / [贴纸] d / [贴纸·happy] d / [动态贴纸] d（to_text_for_ai）
#   官方通道:  裸 [图片]/[视频]/[GIF]…（无描述，落上面的精确表）
# 注意 [链接] 刻意不在表内——链接不是媒体，不该触发媒体块。
_MEDIA_PREFIX_PATTERNS: List = [
    (re.compile(r"^\[图片内容\]\s*(.+)", re.DOTALL), "image"),
    (re.compile(r"^\[图片消息[^\]]*\]\s*(.*)", re.DOTALL), "image"),
    (re.compile(r"^\[图片\]\s*(.+)", re.DOTALL), "image"),
    (re.compile(r"^\[贴纸内容\]\s*(.+)", re.DOTALL), "sticker"),
    (re.compile(r"^\[贴纸(?:·[^\]]*)?\]\s*(.*)", re.DOTALL), "sticker"),
    # Messenger 动态贴纸可带情绪类别（[动态贴纸·love] …）
    (re.compile(r"^\[动态贴纸(?:·[^\]]*)?\]\s*(.*)", re.DOTALL), "animated_sticker"),
    (re.compile(r"^\[LINE贴图\]\s*(.*)", re.DOTALL), "sticker"),
    (re.compile(r"^\[表情\]\s*(.*)", re.DOTALL), "sticker"),
    (re.compile(r"^\[视频内容\]\s*(.+)", re.DOTALL), "video"),
    (re.compile(r"^\[视频消息[^\]]*\]\s*(.*)", re.DOTALL), "video"),
    (re.compile(r"^\[视频\]\s*(.+)", re.DOTALL), "video"),
    (re.compile(r"^\[GIF\]\s*(.*)", re.DOTALL), "gif"),
    (re.compile(r"^\[动图消息[^\]]*\]\s*(.*)", re.DOTALL), "gif"),
    (re.compile(r"^\[动图\]\s*(.*)", re.DOTALL), "gif"),
    (re.compile(r"^\[动态表情\]\s*(.*)", re.DOTALL), "gif"),
    (re.compile(r"^\[语音消息[^\]]*\]\s*(.*)", re.DOTALL), "voice"),
    (re.compile(r"^\[语音\]\s*(.+)", re.DOTALL), "voice"),
    (re.compile(r"^\[文件消息[^\]]*\]\s*(.*)", re.DOTALL), "file"),
    (re.compile(r"^\[文件\]\s*(.+)", re.DOTALL), "file"),
]

# Messenger to_text_for_ai 可能在正文后拼 fusion 提示行——匹配前整体剥掉，
# 防「[图片]\n[上下文提示] …」把提示词当 desc 或让裸占位错过精确表。
_FUSION_HINT_SEP = "\n[上下文提示]"


def _match_media_prefix(text: str) -> tuple:
    """入站文本 → ``(kind, desc)``；非媒体占位 → ``("", "")``。

    先精确匹配裸占位（零描述），再按前缀表提取描述。
    """
    t = (text or "").strip()
    if not t:
        return "", ""
    t = t.split(_FUSION_HINT_SEP)[0].strip()
    # Phase5：带 caption 的视频「说明\n[视频内容] …」（整段不以 [ 开头）
    m_cap = re.search(r"(?:^|\n)\[视频内容\]\s*(.+)", t, re.DOTALL)
    if m_cap:
        return "video", (m_cap.group(1) or "").strip()
    if not t.startswith("["):
        return "", ""
    if t in _PLACEHOLDER_KIND:
        return _PLACEHOLDER_KIND[t], ""
    for rx, kind in _MEDIA_PREFIX_PATTERNS:
        m = rx.match(t)
        if m:
            return kind, (m.group(1) or "").strip()
    if t.startswith("[语音"):
        return "voice", ""
    return "", ""


def _kind_from_text(text: str) -> str:
    return _match_media_prefix(text)[0]


def peer_media_context(
    text: str,
    *,
    media_type: str = "",
    media_ref: str = "",
    media_desc: str = "",
) -> Dict[str, Any]:
    """从入站文本 + 可选媒体字段构造 ``user_context`` 媒体补丁。

    显式 ``media_type``/``media_desc``（结构化来源，如 WA runner / 收件箱行）优先；
    缺失时从文本占位前缀解析（Messenger/LINE/TG A 线的 ``[图片] 描述`` 族），
    使 ai_client 媒体块在**全平台**同口径触发（阶段 3 统一）。
    """
    t = (text or "").strip()
    pk, pdesc = _match_media_prefix(t)
    kind = str(media_type or "").strip().lower() or pk
    desc = (media_desc or "").strip() or pdesc
    if not kind and not desc and not media_ref:
        return {}

    out: Dict[str, Any] = {
        "_peer_message_is_media": True,
        "_media_kind": kind or "media",
    }
    if desc:
        out["_media_desc"] = desc
    if media_ref:
        out["_media_ref"] = str(media_ref)
    # Telegram 收件箱与原生 bot 共用 channel 标记，供 ai_client 多模态 prompt
    out["_inbox_peer_kind"] = kind or "media"
    return out


def build_language_switch_hint(
    history: List[Dict[str, Any]],
    *,
    current_lang: str,
    current_text: str,
) -> str:
    """近几轮用户语系与本轮不同 → 提示模型像真人一样自然跟上（不解释规则）。"""
    from src.ai.translation_service import detect_language

    # 本条到底是什么语种，以**当前文本实际检测**为准——不能只信传入的 current_lang
    # （那是 reply_lang，可能被上一轮锁成 en 等而与本条文本矛盾）。否则会出现"用户明明
    # 说中文，却被提示'突然换成英语啦'"的误判（真机语音场景实测复现）。
    text_lang = (detect_language(current_text) or "").strip()
    cur = text_lang if (text_lang and text_lang != "unknown") else (current_lang or "").strip()
    if not cur or cur in ("unknown", "zh"):
        return ""
    # 一致性护栏：传入 current_lang 与文本实际语种矛盾时，以文本为准（文本已非 zh/unknown）。
    if text_lang and text_lang != "unknown" and text_lang != cur:
        return ""
    prev_langs: List[str] = []
    for m in reversed(history or []):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        c = str(m.get("content") or "").strip()
        if not c or c == (current_text or "").strip():
            continue
        lg = detect_language(c)
        if lg and lg not in ("unknown",):
            prev_langs.append(lg)
        if len(prev_langs) >= 3:
            break
    if not prev_langs:
        return ""
    dominant = prev_langs[0]
    if dominant == cur:
        return ""
    _names = {
        "en": "英语", "ja": "日语", "ko": "韩语", "zh": "中文",
        "es": "西语", "pt": "葡语", "vi": "越南语", "th": "泰语",
    }
    prev_n = _names.get(dominant, dominant)
    cur_n = _names.get(cur, cur)
    return (
        f"【语言切换 · 自然承接】对方刚才主要用「{prev_n}」聊，本条改用了「{cur_n}」。"
        f"请用「{cur_n}」回复，并像真人一样可轻轻点一下这个切换"
        "（例如“突然换成日语啦？”这种自然反应，按语境决定，不要生硬解释语言规则）；"
        "然后直接接住本条内容，保持一致的自然私聊感。"
    )


def build_time_gap_hint(gap_sec: float) -> str:
    """距上一轮对话隔了很久 → 时间感提示（治「把 10 天前当刚才」的幻觉）。

    真实事故（2026-07-13）：用户 10 天后回来发「好呀好呀」，历史窗口里还是 10 天前
    的轮次，AI 说「你刚才说想去大阪玩」「突然换日文了」——旧轮次被当成「刚才」。
    ≥6h 出小时级提示，≥48h 出天级提示；短间隔（正常连聊）返回 ""。纯函数。
    """
    try:
        gap = float(gap_sec or 0)
    except (TypeError, ValueError):
        return ""
    if gap < 6 * 3600:
        return ""
    if gap >= 48 * 3600:
        span = f"{int(gap // 86400)} 天"
    elif gap >= 24 * 3600:
        span = "1 天多"
    else:
        span = f"{int(gap // 3600)} 小时"
    return (
        f"【时间提示——重要】距离你们上一次聊天已经过去约 {span}，对方刚回来。"
        "对话历史里的旧轮次是那时候的，**不是刚才**——绝对不要用「刚才/刚说/你刚才说」"
        "指代旧话题；旧话题要提就带时间感（「前几天你说…」「上次聊到…」），"
        "且只提对方**亲口说过**的内容。像真人一样自然地重新接上，别装作对话从未中断。"
    )


_LANG_COMMENT_RE = re.compile(
    r"换.{0,4}(日文|日语|英文|英语|中文|语言)|日本語に|英語で|switch.{0,12}lang",
    re.IGNORECASE,
)


def build_language_anchor_hint(
    history: List[Dict[str, Any]], *, current_text: str,
) -> str:
    """语言事实钉子（治「无中生有说对方换了语言」的幻觉）。

    真实事故：历史里有旧日语轮次 + AI 自己点评过语言切换，用户发中文「好呀好呀」，
    AI 幻觉「突然换日文了，好可爱！那我也用日文回你！」——幻觉回复进历史后还会
    自我强化。本钉子在「本条是中文 && 历史存在非中文轮次或语言点评痕迹」时注入，
    明确锚定语言事实。条件克制（纯中文历史不注入，防 prompt 膨胀）。纯函数。
    """
    from src.ai.translation_service import detect_language

    t = str(current_text or "").strip()
    if not t:
        return ""
    if (detect_language(t) or "") != "zh":
        return ""
    risky = False
    for m in list(history or [])[-12:]:
        if not isinstance(m, dict):
            continue
        c = str(m.get("content") or "")
        if not c:
            continue
        if m.get("role") == "assistant" and _LANG_COMMENT_RE.search(c):
            risky = True
            break
        # 历史轮次里有日文假名/韩文/明显外语 → 也算风险语境
        if re.search(r"[\u3040-\u309F\u30A0-\u30FF\uAC00-\uD7AF]", c):
            risky = True
            break
    if not risky:
        return ""
    return (
        "【语言事实——锚定】对方**本条消息用的是中文**，并没有切换语言。"
        "不要提「换日文/换英文/换语言」之类的话（哪怕历史里聊过语言切换），"
        "直接用中文自然回应内容本身。"
    )


# 客户「声称往事」的句式（与 scripts/duel_judge.py 的抽取口径同源——那边靠它
# 把「缺席判定」拆成定点是非题，这边靠它决定要不要给 LLM 打预防针）。
_PRIOR_CLAIM_RE = re.compile(
    r"你(?:上次|之前|那天|当初)?(?:不是)?(?:说过|说|答应|承诺|提过|讲过)"
    r"|上次你|之前你|你还记得|我们(?:上次|之前)|咱们(?:上次|之前)"
    r"|你不是要|你说要|你说过要"
    r"|you\s+(?:said|promised|told\s+me)|last\s+time\s+you|remember\s+you",
    re.IGNORECASE,
)
# 声称句式本身的词不算「内容」（否则「你上次说」几个字也参与支持度计算）
_CLAIM_MARKER_RE = re.compile(
    r"你上次|你之前|你那天|你当初|上次你|之前你|你还记得|我们上次|我们之前"
    r"|咱们上次|咱们之前|你不是说过|你不是说|你说过要|你不是要|你说要"
    r"|你说过|你说|不是说|答应|承诺|提过|讲过|对吧|是不是|来着|吗|呢|啊|呀")
# 「完全无据」判据：低于此支持度才敢用强口径（见 claim_support_ratio 的校准记录）
_NO_TRACE_RATIO = 0.25

_CJK_RUN_RE_LOCAL = re.compile(r"[\u4e00-\u9fff]+")
_LATIN_NUM_RE_LOCAL = re.compile(r"[A-Za-z][A-Za-z']{1,}|\d{2,}")
# 虚词/高频语法字：含之的 bigram 视为语法胶水，不参与内容匹配。
# 加这一层是校准逼出来的（见 claim_support_ratio 的两轮数字）：不过滤时
# 「你不是答应过送我一包你店里的手冲豆吗」对「我店里主打手冲豆」只得 0.23
# ——缺的十个 token 全是 一包/不是/你不/过送/的手 这类胶水，而真正的内容
# （手冲/冲豆/店里）明明命中了。刻意**不复用** memory_grounding._content_tokens：
# 那是 Phase8 记忆接地的已校准函数，动它会改记忆写入行为。
_GLUE_CHARS = set(
    "的了是不我你他她它们过把被也就都吗呢啊呀吧嘛个一有在和与要没"
    "这那哪什么么之于对给还又再很太最好上下来去做说想会能可到")


def _claim_tokens(text: str) -> set:
    """内容 token 集合（CJK bigram 去虚词 + 拉丁词/数字）。"""
    out: set = set()
    for run in _CJK_RUN_RE_LOCAL.findall(str(text or "")):
        for i in range(len(run) - 1):
            gram = run[i:i + 2]
            if gram[0] in _GLUE_CHARS or gram[1] in _GLUE_CHARS:
                continue
            out.add(gram)
    out |= {t.lower() for t in _LATIN_NUM_RE_LOCAL.findall(str(text or ""))}
    return out


def claim_support_ratio(claim: str, haystack: str) -> float:
    """声称句的内容 token 有多少比例能在 haystack（历史+长期记忆）里找到。

    ⚠ **这个数只在低端可用，不能当真假判据**。2026-07-29 用真实对练 transcript
    校准（18 条实录编造声称 vs 7 条按苏婉真设定构造的真声称），两轮：

        不过滤虚词：  编造 max=0.58        真声称 min=0.50   ← 分布重叠，无干净阈值
        过滤虚词后：  编造 max=0.89        真声称 min=0.60   ← 低端才分得开

    高区永远会重叠，因为编造句常夹带大量真内容（「你之前说没离开过台北，现在又说
    在宿务开咖啡店」里宿务/咖啡店都是真的），比例被真部分稀释。这件事在裁判侧已经
    证明过一次：自由扫描判真假召回只有 1/3，拆成定点是非题交给模型才稳定 3/3。

    可用的只有低端：``≤0.25`` 一侧有 11/18 编造类、**0 个真声称**（最低 0.60），
    2.4 倍间隔。故本函数只决定提示的**语气强度**；要不要提示由句式决定。
    """
    need = _claim_tokens(_CLAIM_MARKER_RE.sub(" ", str(claim or "")))
    if not need:
        return 1.0          # 取不出内容 token → 无从判断，按有据（用中性口径）
    return len(need & _claim_tokens(haystack)) / float(len(need))


def build_false_premise_hint(
    text: str, *, history: Optional[List[Dict[str, Any]]] = None,
    memory_text: str = "", catalog_facts: str = "",
) -> str:
    """虚假前提提示（治「客户编造往事，人设照单全收还补细节」）。

    实录（2026-07-28 对练，mem_poison 8 轮 **7 处**附和）：客户把从没发生的事
    当既定事实说出来——「你上次说这周要飞大阪」「你新养的猫叫什么来着」「你老公
    也是做电商的对吧」——人设一律认领并主动补细节：「哈哈对，叫豆子」「确实是
    夫妻档，他管供应链我管咖啡店」。编造配偶/宠物这种，对陪伴人设是事故级。
    Phase8 的记忆接地护栏管的是「别把 AI 幻觉**存进**记忆」，管不到「当轮别附和」。

    **设计上刻意不做真假判定**：是否真发生过无法靠文本重叠可靠断定（见
    ``claim_support_ratio`` 的校准负结果），而误判的代价是让人设否认真事
    （「你怎么什么都不记得」也是投诉类）。所以提示是**真假无关**的行为规范——
    先核对记忆、有则承接、无则诚实说不记得、任何情况下不许编细节。这样即使
    句式误命中，也不会造成损害。

    支持度只用来调语气：完全找不到痕迹（≤0.25，该区间零真声称）→ 强口径。
    证据集＝长期记忆 + 近 20 轮历史 + ``catalog_facts``（目录登记的商业事实，
    见下方注释：产品真事实可能压根没在本轮对话出现过）。

    条件克制：客户没用「你说过/答应过/我们上次」这类句式 → 返回 ""。
    「你**刚**说…」刻意不进句式表——那种声称指的是眼前上下文，客户随时在做
    （「你刚说198对吧」），既可核对又无编造空间，收进来只会让提示常驻膨胀。纯函数。
    """
    t = str(text or "").strip()
    if not t or not _PRIOR_CLAIM_RE.search(t):
        return ""
    hay_parts: List[str] = [str(memory_text or "")]
    for m in list(history or [])[-20:]:
        if isinstance(m, dict):
            hay_parts.append(str(m.get("content") or ""))
    # 目录登记的商业事实也是**证据**（2026-07-29 构造性检验逼出来的）：
    # 「你说过注册领7天完整版，是吧」在对话历史里支持度 0.00 —— 但那是目录里真有
    # 的事实，只是这轮对话还没提过；强口径会让人设否认真实的产品事实。
    # 注意这里刻意**不是**「商业类一律降级为中性口径」：那样「你上次答应给我打八折」
    # 也会被放过，而那恰恰是该用强口径的编造（折扣从不授权、目录里查无此事）。
    # 把目录事实并进证据集，两种情况自然分开——真事实有据、编造承诺无据。
    if catalog_facts:
        hay_parts.append(str(catalog_facts))
    ratio = claim_support_ratio(t, "\n".join(hay_parts))
    lead = (
        "【虚假前提警戒——重要】对方这条把「你说过/答应过/我们上次…」当成既定事实。"
    )
    if ratio <= _NO_TRACE_RATIO:
        lead += "而你的长期记忆与本次对话历史里**完全找不到这件事的痕迹**。"
    return (
        lead
        + "请先在记忆与上文里核对：\n"
        "- 确实说过/发生过 → 正常承接，不必多解释。\n"
        "- 记忆里没有 → 像真人一样直接说不记得或反问确认（「我有说过吗？我怎么"
        "不记得」「你是不是记混了」），**绝不能为了迎合就承认**。\n"
        "- 任何情况下都不要顺着对方的说法**编出细节**——时间、名字、地点、承诺、"
        "人物关系（配偶/宠物/亲戚）一旦编出来就会被追问到穿帮，而且会污染后续记忆。\n"
        "拿不准就只回应你确实知道的那部分，不要替对方的记忆背书。"
    )


def _catalog_fact_text() -> str:
    """目录登记的商业事实拼成一段文本（给虚假前提提示当证据）。绝不抛。

    经 ``goals.service.catalog_guard_facts``（与出站守卫同一事实源，
    ``site_catalog`` 自带 mtime 缓存 + 5s stat 节流）。读不到 → ""，
    退化成「只看对话与记忆」的旧行为。
    """
    try:
        from src.config_manager import config_manager  # 局部导入防环
        cfg_root = getattr(config_manager, "config", None) or {}
        cfg_path = getattr(config_manager, "config_path", None)
    except Exception:
        cfg_root, cfg_path = {}, None
    try:
        from src.companion.goals.service import catalog_guard_facts
        facts = catalog_guard_facts(cfg_root, cfg_path) or {}
    except Exception:
        return ""
    parts: List[str] = [str(x) for x in (facts.get("offer_texts") or [])]
    parts += [str(x) for x in (facts.get("catalog_prices") or [])]
    parts += [str(x) for x in (facts.get("offer_free_days") or [])]
    return "\n".join(p for p in parts if p)


def build_short_inbound_hint(text: str) -> str:
    """极短 / 纯语气 / 纯 emoji 入站 → Companion 短回提示（补充 natural_dialogue）。"""
    t = (text or "").strip()
    if not t:
        return (
            "【对方本轮几乎无文字（可能只有表情/贴纸）】"
            "用一两句轻松口语回应氛围即可，不要长篇；可轻轻接梗或问一句很短的跟进。"
        )
    if _is_meaningless_interjection_only(t):
        return (
            "【对方本轮偏语气词/填充音】"
            "像朋友聊天那样短回即可（嗯嗯/哈哈/怎么啦），不要展开成客服式长段或连环提问。"
        )
    # 纯 emoji（去掉 emoji 后无字母数字汉字）
    core = re.sub(
        r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0000FE00-\U0000FE0F"
        r"\U0001F1E0-\U0001F1FF\s]+",
        "",
        t,
    )
    if not core and len(t) <= 12:
        return (
            "【对方本轮主要是表情符号】"
            "回应时宜轻松简短，可回表情或一句口语，不要假装读不懂表情。"
        )
    if len(t) <= 3 and re.search(r"[a-zA-Z]", t):
        return (
            "【对方本轮极短英文】"
            "用同样简短的私聊口吻回应（如 hi→hey / ok→好呀），不要突然变成长篇客服腔。"
        )
    return ""


def apply_inbound_enrichments(
    user_context: Dict[str, Any],
    *,
    text: str,
    history: Optional[List[Dict[str, Any]]] = None,
    reply_lang: str = "",
    media_type: str = "",
    media_ref: str = "",
    media_desc: str = "",
    platform: str = "",
) -> None:
    """就地补全 user_context（供 generate_inbox_draft 调用）。"""
    t = str(text or "").strip()
    user_context["last_message"] = t
    user_context["_current_user_message_for_lang"] = t
    if platform:
        user_context["platform"] = platform
        if platform == "telegram":
            user_context["channel"] = "telegram"

    media_patch = peer_media_context(
        t, media_type=media_type, media_ref=media_ref, media_desc=media_desc,
    )
    user_context.update(media_patch)

    # 三类语境提示汇入 _topic_switch_hint（ai_client 同一消费口）：
    # 语言切换承接 / 语言事实钉子（互斥：本条非中文才可能有前者、是中文才可能有后者）
    # / 时间断层提示（可与前两者叠加）。
    hints: List[str] = []
    hint = build_language_switch_hint(
        list(history or []),
        current_lang=reply_lang,
        current_text=t,
    )
    if hint:
        hints.append(hint)
    anchor = build_language_anchor_hint(list(history or []), current_text=t)
    if anchor:
        hints.append(anchor)
    gap_hint = build_time_gap_hint(user_context.get("_turn_gap_sec") or 0)
    if gap_hint:
        hints.append(gap_hint)
    # 虚假前提（客户声称往事）：与上面三条同一消费口。长期记忆此时已注入
    # （_inject_episodic_into_context 在本函数之前跑），拿它当核对底料；
    # 目录登记的商业事实一并作证据（真产品事实可能这轮还没提过）。
    premise = build_false_premise_hint(
        t, history=list(history or []),
        memory_text=str(user_context.get("_episodic_memory_text") or ""),
        catalog_facts=_catalog_fact_text(),
    )
    if premise:
        hints.append(premise)
    if hints:
        user_context["_topic_switch_hint"] = "\n".join(hints)

    short_hint = build_short_inbound_hint(t)
    if short_hint:
        prev = (user_context.get("_inbound_short_hint") or "").strip()
        user_context["_inbound_short_hint"] = f"{prev}\n{short_hint}".strip() if prev else short_hint


__all__ = [
    "apply_inbound_enrichments",
    "build_language_switch_hint",
    "build_language_anchor_hint",
    "build_time_gap_hint",
    "build_false_premise_hint",
    "claim_support_ratio",
    "build_short_inbound_hint",
    "peer_media_context",
    "media_placeholder",
]
