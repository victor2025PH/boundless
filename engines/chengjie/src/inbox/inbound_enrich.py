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
    "build_short_inbound_hint",
    "peer_media_context",
    "media_placeholder",
]
