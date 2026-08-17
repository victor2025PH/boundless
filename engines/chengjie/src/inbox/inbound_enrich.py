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

    # 已转写语音只标语音块，不标媒体块。A 线 telegram 传 media_type=voice +
    # 空 desc，旧实现 update 把 _peer_message_is_media 置位却不清陈旧
    # _media_desc → 8/01 键盘识图驻留 15 天，8/16 语音问新闻被当成夸键盘
    # （与 protocol_autoreply.media_context_extra 同口径）。
    if kind == "voice":
        try:
            from src.inbox.media_enrich import is_placeholder_only as _ipo
            transcribed = bool(t) and not _ipo(t)
        except Exception:
            transcribed = bool(t)
        if transcribed:
            out: Dict[str, Any] = {"_peer_message_is_voice": True}
            if media_ref:
                out["_media_ref"] = str(media_ref)
            return out

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
    """近几轮用户语系与本轮不同 → 提示模型自然跟上（**绝不指示点破切换**）。

    语种判定一律走 ``lang_policy.evidence_lang``（P0-198，2026-08-03）：系统注入的
    emoji 加注/识图描述不构成客户语言证据；本条**没有语言证据时不做任何切换断言**
    （旧实现拿裸检测的统计兜底当证据，「Haha 😄」也能被判成 en）。

    措辞纪律（两起实锤后收紧）：旧提示鼓励模型「轻轻点一下这个切换（例如"突然换成
    日语啦？"）」——7/31 WhatsApp 与 8/03 Telegram 两起事故里，切换判定本身是被投毒的
    误判，模型照做后变成反咬客户「suddenly switching to English?」。点破错误的代价
    （像 gaslighting）远大于点破正确的收益（可有可无的人味），故一律禁止点破。
    """
    from src.ai.lang_policy import evidence_lang

    # 本条到底是什么语种，以**当前文本的语言证据**为准——不能只信传入的 current_lang
    # （那是 reply_lang，可能被上一轮锁成 en 等而与本条文本矛盾）。无证据 → 不断言。
    cur = evidence_lang(current_text)
    if not cur or cur == "zh":
        return ""
    prev_langs: List[str] = []
    for m in reversed(history or []):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        c = str(m.get("content") or "").strip()
        if not c or c == (current_text or "").strip():
            continue
        lg = evidence_lang(c)
        if lg:
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
        f"请用「{cur_n}」回复，直接自然地接住本条内容本身。"
        "**不要点破、追问或评论这次语言变化**（不要说「怎么换语言了/突然讲某语啦/"
        "switched to English」之类的话——语言判断可能有误，点破错了对方会觉得莫名其妙"
        "甚至被冒犯；真人朋友通常直接跟着对方的语言聊下去）。"
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
    r"换.{0,4}(日文|日语|英文|英语|中文|语言)|(讲|说|飙)起?英[文语]"
    r"|突然.{0,6}(英文|英语|日语|韩语|中文)|日本語に|英語で"
    r"|switch.{0,12}lang|switch(?:ed|ing)?[^.!?\n]{0,24}(english|chinese|japanese|korean|spanish)"
    r"|speaking\s+(english|chinese|japanese|korean)\s+now",
    re.IGNORECASE,
)

_LANG_DISPLAY_NAMES = {
    "en": "英语", "ja": "日语", "ko": "韩语", "zh": "中文",
    "es": "西语", "pt": "葡语", "vi": "越南语", "th": "泰语",
    "id": "印尼语", "ru": "俄语", "fr": "法语", "de": "德语",
}


def _dominant_recent_user_lang(
    history: List[Dict[str, Any]], *, exclude_text: str = "",
) -> str:
    """最近一条**有语言证据**的用户消息语种（与 switch hint 同口径）；取不到返回 ""。

    证据口径经 ``lang_policy.evidence_lang``（P0-198）：系统注入的 emoji 加注
    「（表情：中文）」曾让这里把英文客户判成「主导中文」，联动 switch hint 产出
    「你切英文了」的反咬。
    """
    from src.ai.lang_policy import evidence_lang

    for m in reversed(list(history or [])):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        c = str(m.get("content") or "").strip()
        if not c or c == (exclude_text or "").strip():
            continue
        lg = evidence_lang(c)
        if lg:
            return lg
    return ""


def build_language_anchor_hint(
    history: List[Dict[str, Any]], *, current_text: str,
) -> str:
    """语言事实钉子（治「无中生有说对方换了语言」的幻觉）。

    正向事故（2026-07-13，tg）：历史里有旧日语轮次 + AI 自己点评过语言切换，
    用户发中文「好呀好呀」，AI 幻觉「突然换日文了！那我也用日文回你」。
    反向事故（2026-07-31，198 WhatsApp）：客户**全程英文**，AI 草稿反复出现
    「哈哈，突然跟我讲起英文来了😅」——历史里自己的语言点评进上下文后自我强化，
    旧钉子只认「本条是中文」，反向场景零覆盖。

    新增触发（2026-08-03，198 Telegram 实锤）：**我方自己刚发过错语言的消息**
    ——英文会话里我方错发一条中文后，模型看到 assistant=中文 → user=英文 的相邻
    轮次，会脑补「对方切回英文了」并反咬 "suddenly switching to English?"。
    此时用户语言并没变，必须钉住事实。

    现语义：本条语种可判定（``evidence_lang`` 证据口径）时——
    - 中文：历史含外语轮次或语言点评痕迹 → 锚定「对方用中文，没换语言」（原行为）；
    - 非中文：与近几条用户消息主导语一致（即根本没发生切换），且历史里出现过
      语言点评痕迹 **或 我方最近一条有语言证据的消息语种 ≠ 对方语种**（自己发错过
      语言）→ 锚定「对方一直用 X 语，没换语言，禁止评论语言切换」。
    条件克制（无风险语境不注入，防 prompt 膨胀）。纯函数。
    """
    from src.ai.lang_policy import evidence_lang

    t = str(current_text or "").strip()
    if not t:
        return ""
    cur = evidence_lang(t)
    if not cur:
        return ""

    def _assistant_commented() -> bool:
        for m in list(history or [])[-12:]:
            if (isinstance(m, dict) and m.get("role") == "assistant"
                    and _LANG_COMMENT_RE.search(str(m.get("content") or ""))):
                return True
        return False

    if cur == "zh":
        risky = _assistant_commented()
        if not risky:
            for m in list(history or [])[-12:]:
                if not isinstance(m, dict):
                    continue
                c = str(m.get("content") or "")
                # 历史轮次里有日文假名/韩文/明显外语 → 也算风险语境
                if c and re.search(r"[\u3040-\u309F\u30A0-\u30FF\uAC00-\uD7AF]", c):
                    risky = True
                    break
        if not risky:
            return ""
        return (
            "【语言事实——锚定】对方**本条消息用的是中文**，并没有切换语言。"
            "不要提「换日文/换英文/换语言」之类的话（哪怕历史里聊过语言切换），"
            "直接用中文自然回应内容本身。"
        )

    # 非中文：只有「语言确实没变（正面证据：近几条用户消息主导语==本条）
    # + 自我强化风险语境（AI 点评过语言 / 我方刚发过别的语言）」才注入。
    # 无用户历史证据时宁缺勿错——断言「对方一直用 X 语」必须有据（防对新会话说瞎话）。
    dominant = _dominant_recent_user_lang(history, exclude_text=t)
    if not dominant or dominant != cur:
        return ""  # 无证据 / 真的切换了 → 不锚定（后者交给 switch hint）

    def _assistant_recent_lang() -> str:
        # 最近一条「有语言证据」的 assistant 消息语种（evidence_lang 同口径）。
        # 我方错发中文给英文客户后，正是它 ≠ cur 的形态（2026-08-03 实锤）。
        from src.ai.lang_policy import evidence_lang as _ev
        for m in reversed(list(history or [])[-8:]):
            if isinstance(m, dict) and m.get("role") == "assistant":
                lg = _ev(str(m.get("content") or ""))
                if lg:
                    return lg
        return ""

    a_lang = _assistant_recent_lang()
    if not (_assistant_commented() or (a_lang and a_lang != cur)):
        return ""
    name = _LANG_DISPLAY_NAMES.get(cur, cur)
    return (
        f"【语言事实——锚定】对方**一直在用「{name}」交流**，本条也是「{name}」，"
        "并没有切换语言。历史里若有其它语言的消息或「换语言」的点评，那是我方"
        "发出的或是误判，**不是**对方换语言的证据。不要提「突然讲英文/换语言/"
        f"switched to English」之类的话，直接用「{name}」回应对方消息的内容本身。"
    )


# 语言困惑/抱怨句式（保守窄口径，宁漏勿误——必须配合「错语言语境」前置条件用）：
# 看不懂类 / 问语言类 / english please 类 / why are you speaking 类。
# 刻意不收「什么意思/what does that mean」（多为问内容含义，太宽）。
_LANG_CONFUSION_RE = re.compile(
    r"(?:can'?t|cannot|don'?t|do\s+not)\s+(?:understand|read)\b"
    r"|\bwhat\s+language\b"
    r"|\b(?:in\s+)?english[\s,]*(?:please|pls|plz)\b"
    r"|\bwhy\s+(?:are\s+you\s+)?(?:speaking|writing|texting|typing)\b"
    r"|看不懂|聽不懂|听不懂|读不懂|讀不懂"
    r"|什么语言|什麼語言",
    re.IGNORECASE,
)
# 纯问号消息（≥2 个）：错语言语境下＝典型「你发的这是啥」困惑信号
_PURE_QMARKS_RE = re.compile(r"^[\s!！。.…]*[?？]{2,}[\s?？!！。.…]*$")


def detect_language_complaint(
    text: str, history: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """客户在抱怨/困惑「语言不对」→ 返回 "confusion"；否则 ""（P3-198）。

    **错语言语境是必要条件**：我方最近一条有语言证据的消息语种 ≠ 对方语种时，
    困惑句式才被采信——否则 "can't understand why she left" / 中文会话里的
    「看不懂」（说的是内容）全是误报。8/03 事故里客户收到中文后若发 "??"，
    此前系统毫无感知；这是「客户已经被伤到」的最后观测点 + 恢复话术触发点。
    纯函数，绝不抛。
    """
    t = str(text or "").strip()
    if not t or len(t) > 200:
        return ""
    from src.ai.lang_policy import evidence_lang

    a_lang = ""
    for m in reversed(list(history or [])[-8:]):
        if isinstance(m, dict) and m.get("role") == "assistant":
            a_lang = evidence_lang(str(m.get("content") or ""))
            if a_lang:
                break
    if not a_lang:
        return ""
    u_lang = evidence_lang(t) or _dominant_recent_user_lang(
        history or [], exclude_text=t)
    if not u_lang or u_lang == a_lang:
        return ""
    if _LANG_CONFUSION_RE.search(t) or _PURE_QMARKS_RE.match(t):
        return "confusion"
    return ""


def build_language_recovery_hint() -> str:
    """错语言事故后的恢复话术提示（P3-198）——事故后的恢复动作比事故本身更决定
    信任：真人发错语言会自嘲带过；装没发生（甚至反咬对方）才是信任事故（8/03 实锤）。"""
    return (
        "【语言致歉——重要】对方这条是在表示「看不懂/你用错了语言」——你上一条"
        "消息很可能不是对方的语言。本条回复：先用**对方的语言**说一句很短、自然的"
        "道歉或自嘲（像真人发错聊天窗口那样轻轻带过），然后用对方的语言把上一条的"
        "意思简短重说一遍，再接住对方的内容。绝不辩解、绝不提「系统/翻译」，"
        "也绝不再用错的语言。"
    )


def build_reply_lang_mismatch_hint(
    *, reply_lang: str, current_text: str,
) -> str:
    """草稿语言 ≠ 客户消息语言时的「工作语言说明」（治 198 实锤幻觉）。

    场景：坐席把回复工坊钉在「中文」（或运营 forced_lang），客户全程说英文。
    模型收到「用中文回复」的指令 + 英文消息，会自行脑补「对话本该是中文的，
    对方突然讲英文了」→ 产出「哈哈，突然跟我讲起英文来了😅」这类答非所问
    （198 后端日志 10:18–10:35 至少 8 次）。本提示把「为什么草稿语言与客户
    语言不同」显式讲给模型听，消除脑补空间。纯函数，语种判不出时不注入。
    语种判定走 ``evidence_lang`` 证据口径（P0-198）：系统注入的中文加注不算
    客户语言，无证据的中性消息（Haha/emoji）不做「对方用 X 语」的断言。
    """
    from src.ai.lang_policy import evidence_lang

    target = str(reply_lang or "").strip().lower()
    t = str(current_text or "").strip()
    if not target or not t:
        return ""
    cur = evidence_lang(t)
    if not cur:
        return ""
    if cur.split("-")[0] == target.split("-")[0]:
        return ""
    cur_n = _LANG_DISPLAY_NAMES.get(cur, cur)
    tgt_n = _LANG_DISPLAY_NAMES.get(target, target)
    return (
        f"【工作语言说明】对方本条消息用的是「{cur_n}」，这是对方一贯的正常用语，"
        f"**不是**对方切换了语言；你的回复按当前设定用「{tgt_n}」撰写，"
        "语言面向对方的适配由系统/坐席负责，不用你操心。请直接回答对方消息的"
        "内容本身，禁止评论语言（不要说「你讲英文了/换语言了/我用某语回你」）。"
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

    # 四类语境提示汇入 _topic_switch_hint（ai_client 同一消费口）：
    # 工作语言说明（草稿语言≠客户语言，P0-198）/ 语言切换承接 / 语言事实钉子
    # / 时间断层提示（可与前面叠加）。
    # 工作语言说明命中时**压制**切换承接提示——后者会指示「改用对方语言回复
    # 并点一下切换」，与锁定的草稿语言直接矛盾（正是 198「突然讲英文」幻觉的
    # 温床）；语言锚点仍可叠加（内容不冲突，防历史点评自我强化）。
    hints: List[str] = []
    mismatch = build_reply_lang_mismatch_hint(reply_lang=reply_lang, current_text=t)
    if mismatch:
        hints.append(mismatch)
    else:
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
    # 语言投诉/困惑（P3-198）：计数恒记（错语言伤害的最后观测点）；恢复话术
    # 只在**非钉死工作语言**场景注入——mismatch hint 在场＝运营刻意锁了草稿
    # 语言，「改用对方语言重说」会与之打架，此时只观测、语言决策交给运营。
    try:
        _lc = detect_language_complaint(t, list(history or []))
        if _lc:
            try:
                from src.inbox.outbound_lang_stats import get_outbound_lang_stats
                get_outbound_lang_stats().record(
                    "complaint",
                    conversation_id=str(user_context.get("chat_id") or ""))
            except Exception:
                pass
            if not mismatch:
                hints.append(build_language_recovery_hint())
    except Exception:
        pass
    # 时间断层（P2-198 复活）：Phase8 只在 process_message 链写 _turn_gap_sec，
    # 草稿链恒缺 → 该提示从未生效。键**缺席**时从历史行的可选 ts 推导
    # （normalize_history 已透传）：取「最后一条 != 当前文本的用户消息」时间。
    # A 线显式写过（含 0）→ 不覆盖；无 ts 来源（工作台 DOM）→ 行为不变。
    if "_turn_gap_sec" not in user_context:
        try:
            import time as _t
            prev_ts = 0.0
            for m in reversed(list(history or [])):
                if not isinstance(m, dict) or m.get("role") != "user":
                    continue
                if str(m.get("content") or "").strip() == t:
                    continue
                _mts = float(m.get("ts") or 0)
                if _mts > 0:
                    prev_ts = _mts
                    break
            if prev_ts > 0:
                user_context["_turn_gap_sec"] = max(0.0, _t.time() - prev_ts)
        except Exception:
            pass
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
    # 自述一致性锚点（P1-198 问题6）：AI 在本会话亲口说过的个人事实（年龄/
    # 婚姻/家庭/职业/居住）原句引用回注——情景记忆刻意不记 AI 侧（Phase8
    # 反幻觉），bio 检索漏检时自述就漂移（实锤：说过 Divorced 隔几十条又变卦）。
    try:
        from src.inbox.self_claims import build_self_claims_hint
        sc_hint = build_self_claims_hint(list(history or []))
        if sc_hint:
            hints.append(sc_hint)
    except Exception:
        pass
    # AI 质疑应对（2026-08-03，AI 味周报闭环）：客户质疑「你是AI/机器人」或吐槽
    # 「机器味」→ 注入应对要点（别否认三连/别自证/别突然热情），并进程计数供观测。
    # 词表保守（宁漏勿误，正常聊 AI 工具不命中），与周报离线口径同源
    # （src/utils/ai_suspicion 单一事实源）。
    try:
        from src.utils.ai_suspicion import (
            build_suspicion_hint,
            detect_ai_suspicion,
            record_suspicion,
        )
        _sus_kind = detect_ai_suspicion(t)
        if _sus_kind:
            record_suspicion(_sus_kind, t)
            _sus_hint = build_suspicion_hint(_sus_kind, t)
            if _sus_hint:
                hints.append(_sus_hint)
    except Exception:
        pass
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
    "build_reply_lang_mismatch_hint",
    "build_time_gap_hint",
    "build_false_premise_hint",
    "claim_support_ratio",
    "build_short_inbound_hint",
    "peer_media_context",
    "media_placeholder",
]
