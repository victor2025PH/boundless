# -*- coding: utf-8 -*-
"""统一 STOP 硬闸（智安 P0-2，2026-10-08）。

红线（用户原话）：「用户发 STOP 后继续发」——只留痕、绝不真发。

本模块**不造第二套**停联机制，只把主线已有的三件打通并补齐缺口：

- 词表：唯一判定入口仍是 ``src.ai.chat_assistant_service._stop_contact_hit``（``quick_analyze`` →
  ``risk_reasons`` 含 ``stop_contact``）。本模块提供它此前缺的**多语补充词表**
  :func:`match_lexicon`——单独一个 ``STOP`` / ``Stop po`` / ``tigil na`` / ``ayoko na`` /
  ``huwag mo na akong i-chat`` / Bisaya / 印地语 / 中文「退订」等——由 ``_stop_contact_hit``
  在旧英文 / 中文正则之后调用。所以 ``stop_contact_hits``、``autosend_policy.hard_stop_reason``、
  ``risk_grader``、协议链、收件箱草稿链看到的是**同一份**判定。
- 名单：沿用会话冻结（``stop_contact.frozen_reason``）+ 账号级停联名单
  （``account_blocklist``）。本模块只加一个跨账号视角：同一 ``platform + external_id``、
  或同一手机号（WhatsApp JID / phone:xxx）在任一账号上要求过停联，就算已停联。
- 默认硬停：``risk_grader`` 把 ``stop_contact`` 视为**隐含锁定**（``locked_by="stop_gate"``），
  不再依赖运营在「敏感话题」卡手动锁定。只有显式 ``compliance.stop_gate.enabled: false``
  才回到 R88 的「只记录」旧行为（应急开关，生产不应关）。
- 审计：``account_blocklist.db`` 里的 ``stop_gate_audit`` 表，每次拦截 / 识别记一行
  （路径、动作、平台、账号、对端、会话、原因、命中词）。**不记消息原文**，命中词截 40 字。

接入点（出站前都查名单，拦下只留痕）：
- ``replybus decide`` 入口：入站命中 → 冻结 + 进名单 + ``silent`` / ``reason=stop``，只附一条
  模板确认（``confirm_text``。默认 ``stop_contact.farewell_text``；只有
  ``compliance.stop_gate.multilingual_confirm`` 显式打开时，tl / ceb / hi 才改用
  snippets 里的 persona 句，en / zh 仍用 farewell。不走 LLM，且每个停联只给一次）；
  已停联的对端 → ``silent`` / ``reason=stop``。
- ``protocol_autoreply.run_autoreply``：生成前查名单；入站命中停联按隐含锁定硬停。
- 收件箱自动发送：``drafts.auto_generate_draft`` 起草前查名单；``autosend_policy`` 经词表 + 隐含锁定硬停。
- 主动触达：``proactive_peer_hygiene.proactive_candidate_ok`` 与 ``proactive_topic._send`` 发送前查。

本模块全部函数绝不抛：判定异常按「未命中」处理，但**名单查询异常不会放大成误发**——
调用方本来就在既有闸之后，这里只是多一道。
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: decide / 审计统一原因码
STOP_REASON = "stop"
#: 冻结原因（＝ stop_contact.FREEZE_REASONS[0]）
FREEZE_REASON = "stop_contact"
#: 隐含锁定来源码（risk_grader.is_locked_effective 的 by）
LOCKED_BY = "stop_gate"

# ═══════════════════════════════════════════════════════════════════════
# ① 多语补充词表
# ═══════════════════════════════════════════════════════════════════════
#
# 两类规则：
# A. **整句**（规范化后整条消息恰好等于）——短指令式：STOP / Stop po / tigil na / ayoko na /
#    unsubscribe / 退订 / बंद करो。只认整句是为了不误伤「don't stop」「stop it, you're making me
#    laugh」「ayoko na ng adobo」这类句中用法。
# B. **短语**（句中任意位置，ASCII 词边界）——带「对我 / 发消息」宾语的明确拒绝：
#    huwag mo na akong i-chat / ayaw na ko i-message / message mat karo / remove me from your list。
#
# 规范化：NFKC → 小写 → 去掉表情 / 标点（保留字母、数字、空格、连字符、撇号、CJK、天城文）→ 压空白。
# 礼貌尾词（po / pls / please / na / lang / ha / bro / sis / 请 / 谢谢 / please 等）在整句判定前剥掉，
# 所以「STOP!!! 🙏」「Stop po」「stop na po pls」「please STOP」都归一成 stop。

_POLITE_TAIL = (
    "po", "pls", "plz", "please", "lang", "ha", "bro", "sis", "ok", "okay", "kayo", "ka",
    "thanks", "thank you", "ty", "now", "already", "nalang", "sana", "salamat", "talaga",
    "ji", "bhai", "yaar", "palihug", "palihog", "pls lang", "请", "谢谢", "了", "吧", "啊", "啦",
)
_POLITE_HEAD = ("please", "pls", "plz", "po", "hoy", "uy", "bro", "sis", "请")

# A. 整句（规范化 + 剥礼貌词后）
_WHOLE: Tuple[str, ...] = (
    # en（SMS 行业惯例 STOP / STOPALL / UNSUBSCRIBE；不收 cancel / end / quit——聊天里歧义太大）
    "stop", "stopall", "stop all", "stop pls", "stop please", "stop messaging", "stop texting",
    "stop sending", "stop the messages", "no more messages", "no more msgs",
    "unsubscribe", "unsub", "unsubscribe me", "opt out", "opt-out", "optout", "opt me out",
    "remove me", "stop contacting", "dont message", "don't message", "dont text", "don't text",
    # 智语 2026-10-08：enough 单独成句按停联（CANCEL / END / QUIT 见下方 _STRICT_WHOLE：只认裸词）
    "enough",
    # tl / Taglish
    "tigil", "tigil na", "tigilan", "tigilan mo na", "tigilan mo na ako", "tigilan nyo na ako",
    "tama na", "ayoko na", "ayaw ko na", "stop na", "stop mo na", "stop nyo na", "stop niyo na", "itigil mo na", "itigil nyo na",
    "wag mo na ako i-chat", "unsubscribe na", "pakitigil", "pakitigil na", "pakihinto na", "enough na",
    # Bisaya / Cebuano
    "hunong na", "hunong", "undang na", "undang", "ayaw na", "ayaw na ko", "ayaw nako",
    "ayaw na pag message", "ayaw na pag-message", "hunonga", "hunonga na", "pahunong", "pahunong na",
    "undanga na", "paundang na",
    # hi / Hinglish（天城文 + 罗马化）
    "band karo", "bandh karo", "band kro", "bas karo", "mat bhejo", "message mat karo",
    "msg mat karo", "बंद करो", "बस करो", "मत भेजो", "मैसेज मत करो", "मैसेज मत भेजो",
    "अनसब्सक्राइब", "unsubscribe karo", "band karo messages", "band karo message",
    # zh（旧正则已覆盖「别再发了 / 不要再联系」等，这里补短指令；TD / T 是国内短信退订惯例）
    "停", "停止", "退订", "取消订阅", "td", "别发了", "不要发了", "不要再发",
    "停止发送", "停发", "别再发送", "不要再发送",
)
_WHOLE_SET = frozenset(_WHOLE)

# 蛋博士 2026-10-08 拍板：整条消息**只有**一个 CANCEL / END / QUIT（大小写不限，前后只允许标点 / 空格）
# → 按短信行业惯例（CTIA）硬停，与 STOP / UNSUBSCRIBE / STOPALL 同等；「STOP 后继续发」是红线，宁可多拦。
# 不剥礼貌词（「please cancel」「cancel po」不在此列，走句中规则 / review_hint）；句中出现（cancel my order、
# the end）同样不在此列。
_STRICT_WHOLE = frozenset({"cancel", "end", "quit"})
_STRICT_STRIP = re.compile(r"^[\W_]+|[\W_]+$", re.UNICODE)


def _strict_whole_hit(text: Any) -> str:
    try:
        t = unicodedata.normalize("NFKC", str(text or "")).strip()
        t = _STRICT_STRIP.sub("", t).lower()
        return t if t in _STRICT_WHOLE else ""
    except Exception:
        return ""

_MSG = (r"(?:i-?)?(?:chat|message|msg|mesg|text|txt|pm|dm|kontakin|kontakon|contactin|kontak|contact"
        r"|tawagan|tawag|istorbohin|guluhin|spam)")
# B. 句中短语（ASCII 词边界 lookaround；中文 / 天城文用子串）
_PHRASES = re.compile(
    r"(?<![a-z0-9_])(?:"
    # ── tl / Taglish ──
    r"(?:h?uwag|wag|'wag)\s+(?:mo|nyo|niyo|n'yo|ninyo)\s+(?:na\s+)?(?:akong|ako|kami|kaming)\s+" + _MSG
    + r"|(?:h?uwag|wag|'wag)\s+(?:mo|nyo|niyo)\s+na\s+(?:akong|ako)\s+(?:i-?)?(?:chat|message|text|pm)"
    r"|(?:h?uwag|wag)\s+(?:ka|kayo)\s+na\s+(?:mag-?)?(?:chat|message|text|msg|pm)"
    r"|(?:h?uwag|wag)\s+na\s+(?:kayong|kang)\s+(?:mag-?)?(?:chat|message|text|msg|pm)"
    r"|tigil(?:an)?\s+(?:mo|nyo|niyo)?\s*na\s+(?:ako|ang\s+(?:pag[-\s]?)?(?:chat|message|text))"
    r"|tigil(?:an)?\s+(?:mo|nyo|niyo)\s+na\s+(?:ang\s+)?(?:pag[-\s]?)?(?:chat|message|text|pagmemessage|pagtext)"
    r"|ayoko\s+na\s+(?:ng|sa|ma-?)\s*(?:chat|message|messages|text|texts|msg|mga\s+message|makatanggap)"
    r"|ayaw\s+ko\s+na\s+(?:ng|sa|ma-?)\s*(?:chat|message|messages|text|msg|makatanggap)"
    r"|stop\s+(?:mo|nyo|niyo)\s+na\s+(?:ang\s+)?(?:pag[-\s]?)?(?:chat|message|text|pagmemessage)"
    r"|(?:h?uwag|wag)\s+(?:mo|nyo|niyo)\s+na\s+(?:akong|ako)\s+(?:i-?)?(?:istorbohin|guluhin|kulitin)"
    # 智语 2026-10-08：句中带 po / mag-send / 名单删除 / 不想再收
    r"|(?:h?uwag|wag)\s+na\s+(?:po\s+)?(?:kayong|kang|kayo|ka)\s+(?:po\s+)?(?:mag-?)?(?:send|chat|message|text|msg|pm|txt)"
    r"|(?:h?uwag|wag)\s+(?:mo|nyo|niyo)\s+na\s+(?:po\s+)?(?:akong|ako)\s+" + _MSG
    + r"|(?:ayoko|ayaw\s+ko)\s+na\s+(?:po\s+)?(?:(?:ng|sa)\s+)?(?:makatanggap|matanggap|ma-?(?:message|chat|text))"
    r"|(?:alisin|tanggalin|pakitanggal|pakialis|tanggal|alis)\s+(?:mo\s+|nyo\s+|niyo\s+)?(?:na\s+)?(?:po\s+)?"
    r"(?:ako|kami)\s+sa\s+(?:listahan|list|contacts?|contact\s+list)"
    r"|stop\s+na\s+(?:po\s+)?(?:sa\s+)?(?:pag[-\s]?)?(?:chat|text|message|txt|msg|pagtext|pagmessage)"
    r"|(?:stop|tigil)(?:\s+it)?(?:\s+na)?(?:\s+po)?\s+(?:i'?m\s+|im\s+)?(?:not|di|hindi)\s+(?:po\s+)?(?:ako\s+)?(?:interested|interesado)"
    # ── Bisaya ──
    r"|ayaw\s+(?:na\s+)?(?:ko|ko'g|ko\s+og|nako|mi|kami)\s+(?:i-?|pag-?|pag\s+)?(?:message|chat|text|txt|msg|pm|hasla|hasola|hasolon|samoka)"
    r"|ayaw\s+na\s+(?:mo|kamo|kayo|ka)\s+(?:pag-?|pag\s+)?(?:message|chat|text|txt|msg)"
    r"|ayaw\s+na\s+(?:pag[-\s]?|og\s+)?(?:message|chat|text|txt|msg)"
    r"|(?:hunong|undang)\s+na\s+(?:sa\s+)?(?:pag[-\s]?)?(?:message|chat|text|txt)"
    r"|(?:dili|di)\s+na\s+ko\s+(?:gusto|ganahan)\s+(?:og\s+|ug\s+|sa\s+|ma-?)(?:message|chat|text|ma-?message)"
    # ── en（旧正则之外的漏网）──
    r"|(?:stop|quit)\s+(?:sending|messaging|texting|contacting|spamming|dming|pming)\b"
    r"|no\s+more\s+(?:messages|texts|msgs|chats)"
    r"|(?:remove|take)\s+me\s+(?:off|from)\s+(?:your|this|the)\s+(?:list|contacts?|mailing\s+list)"
    r"|(?:i\s+(?:want|wanna|would\s+like)\s+to\s+)?unsubscribe(?:\s+me)?"
    r"|opt\s*-?\s*(?:me\s+)?out"
    r"|i\s+(?:don'?t|do\s+not)\s+want\s+(?:(?:any\s+more|anymore|any|more|these|those|your)\s+)*(?:messages|texts|msgs)"
    # ── hi 罗马化 ──
    r"|(?:mujhe|muje|mjhe)\s+(?:message|msg|messages|text)\s+(?:mat|na|mt)\s+(?:karo|kro|bhejo|kariye|kijiye|bhejna)"
    r"|(?:message|msg|messages|text)\s+(?:mat|mt)\s+(?:karo|kro|bhejo|kariye|kijiye|bhejna)"
    r"|pareshan\s+(?:mat|mt)\s+(?:karo|kro|kijiye)"
    r"|(?:band|bandh)\s+(?:karo|kro|kardo|kar\s+do)\s+(?:messages?|msgs?|texts?)"
    r"|(?:messages?|msgs?|texts?)\s+(?:bhejna|karna)\s+(?:band|bandh)\s+(?:karo|kro|kardo|kar\s+do)"
    r")(?![a-z0-9_])"
    # ── 天城文 / 中文：子串 ──
    r"|मुझे\s*(?:मैसेज|संदेश|मेसेज)\s*(?:मत|न)\s*(?:करो|करें|भेजो|भेजें|कीजिए)"
    r"|(?:मैसेज|संदेश|मेसेज)\s*(?:मत|न)\s*(?:करो|करें|भेजो|भेजें|कीजिए)"
    r"|परेशान\s*मत\s*(?:करो|करें|कीजिए)|संपर्क\s*मत\s*(?:करो|करें|कीजिए)"
    r"|(?:मैसेज|संदेश|मेसेज)\s*(?:भेजना|करना)\s*बंद\s*(?:करो|करें|कीजिए|कर\s*दो)"
    r"|(?:मैसेज|संदेश|मेसेज)\s*नहीं\s*चाहिए|(?:लिस्ट|सूची)\s*से\s*(?:हटाओ|हटा\s*दो|हटाएं|निकालो)|अनसब्सक्राइब"
    # 退订：排除「退订单 / 退订货 / 退订金 / 退订房 / 退订机票……」（退单退款不是停联，智语 2026-10-08）
    r"|退订(?!的?[单货款金房酒机票座位餐车船团])|取消订阅|别再给我发|不要再给我发|别给我发消息|不要给我发消息"
    r"|停止发送|停发(?:消息|信息|短信)|(?:以后)?(?:别|不要)(?:再)?给我发(?:消息|信息|短信)?了",
    re.IGNORECASE,
)

# 句中否定 / 转述——命中则不认 B 类短语（「he told me to stop messaging him」由旧正则处理，这里只防一种）
_PHRASE_NEGATED = re.compile(
    r"(?<![a-z])(?:don'?t|do\s+not|never|didn'?t|won'?t)\s+(?:want\s+to\s+|wanna\s+)?unsubscribe"
    r"|(?:how\s+(?:do|can|to)\s+i|paano)\s+(?:mag-?)?unsubscribe",
    re.IGNORECASE,
)

_KEEP = re.compile(r"[^0-9a-z\u00c0-\u024f\u0900-\u097f\u3400-\u9fff\uf900-\ufaff\s'\-]+")


def normalize(text: Any) -> str:
    """NFKC + 小写 + 去表情 / 标点（保留拉丁、天城文、CJK、撇号、连字符）+ 压空白。纯函数。"""
    try:
        s = unicodedata.normalize("NFKC", str(text or "")).lower()
    except Exception:
        s = str(text or "").lower()
    s = s.replace("’", "'").replace("‘", "'")
    s = _KEEP.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip(" -'")


def _polite_variants(s: str) -> List[str]:
    """逐步剥掉首尾礼貌 / 语气词，返回每一步的形态（含原形）；用于整句判定。

    「ayoko na po」→ [ayoko na po, ayoko na]；「please STOP pls」→ [please stop pls, please stop, stop]。
    """
    out: List[str] = []

    def _dedup(x: str) -> str:
        parts = x.split(" ")
        if len(parts) > 1 and len(set(parts)) == 1:      # 「stop stop stop」→ stop
            return parts[0]
        if x and not x.isascii() and " " not in x and len(set(x)) == 1 and len(x) <= 4:
            return x[0]                                   # 「停停停」→ 停
        return x

    cur = s
    for _ in range(8):
        if not cur:
            break
        d = _dedup(cur)
        for v in (cur, d):
            if v and v not in out:
                out.append(v)
        nxt = cur
        for tail in sorted(_POLITE_TAIL, key=len, reverse=True):
            if nxt.endswith(" " + tail):
                nxt = nxt[: -len(tail) - 1].strip()
                break
            if not tail.isascii() and nxt.endswith(tail) and len(nxt) > len(tail):
                nxt = nxt[: -len(tail)].strip()
                break
        if nxt == cur:
            for head in _POLITE_HEAD:
                if nxt.startswith(head + " "):
                    nxt = nxt[len(head) + 1:].strip()
                    break
        if nxt == cur:
            break
        cur = nxt
    return out


def _whole_hit(seg: str) -> str:
    for v in _polite_variants(seg):
        if v in _WHOLE_SET:
            return v
    return ""


def _is_filler(seg: str) -> bool:
    """整段都是礼貌 / 语气词（「salamat」「thanks po」）→ True。"""
    vs = _polite_variants(seg)
    return bool(vs) and all(
        (v in _POLITE_TAIL or v in _POLITE_HEAD) for v in vs[-1:])


def match_lexicon(text: Any) -> str:
    """多语补充词表命中 → 命中词（规范化后）；未命中 → ""。纯函数、绝不抛。

    由 ``chat_assistant_service._stop_contact_hit`` 在旧英文 / 中文正则之后调用；
    直接调用也可（测试 / 审计）。
    """
    try:
        strict = _strict_whole_hit(text)
        if strict:
            return strict
        s = normalize(text)
        if not s:
            return ""
        hit = _whole_hit(s)
        if hit:
            return hit
        # 多句：每一句都是停联短指令或纯礼貌词（「STOP. Ayoko na.」「Stop po. Salamat」）才算；
        # 「Stop! haha you're so funny」这种玩笑不算。
        segs = [normalize(x) for x in re.split(r"[.!?。！？\n,，;；]+", str(text or ""))]
        segs = [x for x in segs if x]
        if len(segs) > 1:
            seg_hits = [_whole_hit(x) for x in segs]
            if any(seg_hits) and all(h or _is_filler(x) for h, x in zip(seg_hits, segs)):
                return next(h for h in seg_hits if h)
        if _PHRASE_NEGATED.search(s):
            return ""
        m = _PHRASES.search(s)
        if m:
            return re.sub(r"\s+", " ", m.group(0)).strip()[:40]
    except Exception:
        logger.debug("[stop-gate] 词表判定异常（按未命中）", exc_info=True)
    return ""


def detect(text: Any) -> str:
    """统一判定（与 ``quick_analyze`` 同一入口）：命中 → 命中词；否则 ""。绝不抛。"""
    t = str(text or "").strip()
    if not t:
        return ""
    try:
        from src.ai.chat_assistant_service import _stop_contact_hit
        return str(_stop_contact_hit(t.lower()) or "")
    except Exception:
        return match_lexicon(t)


def is_stop_message(text: Any) -> bool:
    return bool(detect(text))


# ── 含糊停联信号 → 待人工确认（智语 2026-10-08，「宁可多拦」）─────────────────────
#
# 不够硬停（不冻结、不进名单、不发确认），但出现了停联 / 退出类关键词，又不属于已知无害用法：
# 「stop it haha」「omg stop」「should I stop playing?」「cancel na lang」「取消」「别烦」……
# 调用方据此**不自动回复**，打「需人工」让坐席确认：replybus → silent/reason=stop_review；
# 收件箱 → 草稿降 L1 + needs_human；协议直发链 → 不生成、只提醒坐席。
REVIEW_REASON = "stop_review"

_REVIEW_KEYS = re.compile(
    r"(?<![a-z0-9_])(?:stop+|stopping|tigil|itigil|hinto|hunong|undang|unsub(?:scribe)?|"
    r"opt\s*-?\s*out|cancel|quit|enough|block)(?![a-z0-9_])"
    r"|停止|停发|退订(?!的?[单货款金房酒机票座位餐车船团])|取消|别烦|烦死|拉黑|屏蔽|别发(?![呆烧胖福财愁火疯抖誓脾])|不要发(?![呆烧胖福财愁火疯抖誓脾])|别再发(?![呆烧胖福财愁火疯抖])|不要再发(?![呆烧胖福财愁火疯抖])"
    r"|बंद\s*कर|अनसब्सक्राइब|मत\s*भेजो",
    re.IGNORECASE,
)
# 已知无害用法（命中关键词但明显不是要我们停）：剥掉这些片段后再看还剩不剩关键词。
_REVIEW_BENIGN = re.compile(
    r"(?:bus|pit|full|jeep|tricycle|last|next|first)\s+stop|tigil-?\s*pasada|"
    r"(?:quit|stop)\s+(?:my|his|her|the\s+job|smoking|drinking|eating|crying|laughing|worrying|overthinking|thinking)\b|stop\s*over|stop\s+(?:by|in|at|sign|light)|non-?stop|one-?stop|"
    r"(?:don'?t|do\s+not|didn'?t|did\s+not|never|can'?t|cannot|couldn'?t|won'?t|wouldn'?t|not)\s+"
    r"(?:want\s+to\s+|wanna\s+|really\s+|ever\s+|gonna\s+)*stop|"
    r"cancel(?:l?ed|ling)?\s+(?:my|the|this|that|our|an?|his|her)\s+(?:order|booking|reservation|appointment|"
    r"flight|ticket|trip|meeting|plan|plans|payment|transaction|deposit|withdrawal)|"
    r"(?:order|booking|reservation|appointment|payment)\s+(?:was\s+|is\s+|got\s+)?cancel(?:l?ed)?|"
    r"取消(?:订单|预订|预约|付款|支付|交易|航班|行程|会议|发货|退款|提现|充值)|(?:订单|预约|预订)\S{0,4}取消|"
    r"block\s*chain|(?:pa-?)?cancel\w*\s+(?:po\s+)?(?:ng\s+|yung\s+|ang\s+|the\s+|my\s+)?(?:order|booking|reservation|appointment)|"
    r"hindi\s+(?:ako\s+)?(?:titigil|hihinto)",
    re.IGNORECASE,
)


#: 整句（剥礼貌词后）恰好是这些 → 含糊停联（短信行业 STOP 同义词，聊天里可能指取消订单 / 结束话题）
_REVIEW_WHOLE = frozenset({"cancel", "end", "quit", "stop it", "end it", "cancel na", "quit na", "please cancel",
                           "cancel po", "end po", "quit po", "取消", "算了别发"})


def review_hint(text: Any) -> str:
    """含糊停联信号 → 命中关键词（≤40 字）；硬停（:func:`detect` 命中）或无害用法 → ""。绝不抛。"""
    try:
        t = str(text or "").strip()
        if not t or detect(t):
            return ""
        for v in _polite_variants(normalize(t)):
            if v in _REVIEW_WHOLE:
                return v
        s = unicodedata.normalize("NFKC", t).lower().replace("\u2019", "'")
        s = _REVIEW_BENIGN.sub(" ", s)
        m = _REVIEW_KEYS.search(s)
        if m:
            return re.sub(r"\s+", " ", m.group(0)).strip()[:40]
    except Exception:
        logger.debug("[stop-gate] review 判定异常（按未命中）", exc_info=True)
    return ""


# ═══════════════════════════════════════════════════════════════════════
# ② 开关
# ═══════════════════════════════════════════════════════════════════════

def gate_cfg(config: Any = None) -> Dict[str, Any]:
    try:
        c = config if isinstance(config, dict) else (getattr(config, "config", None) or {})
        node = ((c or {}).get("compliance") or {}).get("stop_gate")
        return node if isinstance(node, dict) else {}
    except Exception:
        return {}


def enforced(config: Any = None) -> bool:
    """STOP 硬闸是否生效。**默认 True**；只有显式 ``compliance.stop_gate.enabled: false`` 才关。"""
    v = gate_cfg(config).get("enabled", True)
    if isinstance(v, str):
        return v.strip().lower() not in ("0", "false", "no", "off")
    return v is not False


# ═══════════════════════════════════════════════════════════════════════
# ③ 名单（复用 stop_contact 冻结 + account_blocklist）
# ═══════════════════════════════════════════════════════════════════════

def phone_key(value: Any) -> str:
    """手机号 / WA JID / ``phone:xxx`` → 末 10 位数字（不足 7 位 → ""）。纯函数。"""
    s = str(value or "")
    if "@" in s:
        s = s.split("@", 1)[0]
    if ":" in s and s.split(":", 1)[0].isalpha():
        s = s.split(":", 1)[1]
    s = s.split(":", 1)[0]  # JID 设备后缀 6391xxxx:12@s.whatsapp.net
    d = re.sub(r"\D", "", s)
    if len(d) < 7:
        return ""
    return d[-10:]


def _split_cid(cid: str) -> Tuple[str, str, str]:
    parts = str(cid or "").split(":", 2)
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    return "", "", ""


def _conv_id(platform: str, account_id: str, peer: str) -> str:
    try:
        from src.inbox.normalizer import conv_id
        return conv_id(str(platform or ""), str(account_id or ""), str(peer or ""))
    except Exception:
        return f"{platform}:{account_id}:{peer}"


def _blocklist(store: Any = None):
    try:
        from src.inbox.account_blocklist import get_blocklist
        return get_blocklist(store)
    except Exception:
        return None


def contact_stopped(
    store: Any,
    platform: str,
    account_id: str = "",
    peer: str = "",
    *,
    conversation_id: str = "",
    phone: str = "",
) -> str:
    """该对端是否已要求停联 → 命中来源码；否则 ""。只读、绝不抛。

    来源码：``frozen``（会话冻结）/ ``blocklist``（本账号名单）/ ``blocklist_peer``（同平台
    同 external_id 在别的账号上停联过）/ ``blocklist_phone``（同一手机号在任一平台账号上停联过）。
    """
    plat = str(platform or "").strip().lower()
    acct = str(account_id or "").strip()
    pr = str(peer or "").strip()
    cid = str(conversation_id or "").strip()
    if not (plat and pr) and cid:
        p2, a2, k2 = _split_cid(cid)
        plat, acct, pr = plat or p2.lower(), acct or a2, pr or k2
    try:
        if store is not None:
            from src.inbox.stop_contact import frozen_reason
            c = cid or (_conv_id(plat, acct, pr) if plat and acct and pr else "")
            if c and frozen_reason(store, c) == FREEZE_REASON:
                return "frozen"
    except Exception:
        logger.debug("[stop-gate] frozen_reason 异常", exc_info=True)
    bl = _blocklist(store)
    if bl is None or not plat or not pr:
        return ""
    try:
        if acct and bl.is_blocked(plat, acct, pr):
            return "blocklist"
        if bl.is_peer_blocked_any_account(plat, pr):
            return "blocklist_peer"
        pk = phone_key(phone) or (phone_key(pr) if plat in ("whatsapp", "sms", "viber", "phone") else "")
        if pk and bl.is_phone_blocked(pk):
            return "blocklist_phone"
    except Exception:
        logger.debug("[stop-gate] 名单查询异常", exc_info=True)
    return ""


def record_stop(
    store: Any,
    *,
    platform: str,
    account_id: str,
    peer: str,
    conversation_id: str = "",
    hit: str = "",
    source: str = "stop_gate",
    chat_name: str = "",
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """登记一次停联：会话冻结（``freeze_conversation``，已含进账号级名单）+ 名单兜底写入。
    幂等、绝不抛。返回 ``{conversation_id, frozen, listed, already}``。"""
    plat = str(platform or "").strip().lower()
    acct = str(account_id or "").strip() or "_"
    pr = str(peer or "").strip()
    cid = str(conversation_id or "").strip() or _conv_id(plat, acct, pr)
    out: Dict[str, Any] = {"conversation_id": cid, "frozen": False, "listed": False,
                           "already": bool(contact_stopped(store, plat, acct, pr, conversation_id=cid))}
    hits = [str(hit)[:40]] if hit else ["stop"]
    # 会话不在本机收件箱（replybus 执行层的对端、别的实例的号）→ 只进名单，不凭空造会话
    conv_known = store is not None
    if store is not None and hasattr(store, "get_conversation"):
        try:
            conv_known = bool(store.get_conversation(cid))
        except Exception:
            conv_known = False
    out["conv_known"] = conv_known
    if conv_known:
        try:
            from src.inbox.stop_contact import freeze_conversation
            r = freeze_conversation(store, platform=plat, account_id=acct, chat_key=pr,
                                    conversation_id=cid, reason=FREEZE_REASON, hits=hits,
                                    chat_name=chat_name, now=now)
            out["frozen"] = bool(r)
        except Exception:
            logger.debug("[stop-gate] freeze 失败（继续写名单）", exc_info=True)
    bl = _blocklist(store)
    if bl is not None and plat and pr:
        try:
            bl.add(plat, acct, pr, reason=FREEZE_REASON, hit_text="|".join(hits), ts=now,
                   source=str(source or "stop_gate")[:40])
            out["listed"] = True
        except Exception:
            logger.debug("[stop-gate] 名单写入失败", exc_info=True)
    return out


# ═══════════════════════════════════════════════════════════════════════
# ④ 审计
# ═══════════════════════════════════════════════════════════════════════

def audit(
    store: Any = None,
    *,
    path: str,
    action: str,
    platform: str = "",
    account_id: str = "",
    peer: str = "",
    conversation_id: str = "",
    reason: str = STOP_REASON,
    hit: str = "",
    now: Optional[float] = None,
) -> bool:
    """审计表 ``stop_gate_audit`` 记一行（不记原文）+ 日志一行 ``[stop-gate]``。绝不抛。

    ``action``：``detected``（入站识别）/ ``blocked``（出站被拦）/ ``confirm``（放行唯一一条模板确认）。
    """
    ts = float(now if now is not None else time.time())
    try:
        logger.info("[stop-gate] path=%s action=%s reason=%s conv=%s hit=%s",
                    path or "-", action or "-", reason or "-",
                    conversation_id or f"{platform}:{account_id}:{peer}", (hit or "-")[:40])
    except Exception:
        pass
    bl = _blocklist(store)
    if bl is None:
        return False
    try:
        return bool(bl.audit(ts=ts, path=path, action=action, platform=platform,
                             account_id=account_id, peer=peer, conversation_id=conversation_id,
                             reason=reason, hit=hit))
    except Exception:
        logger.debug("[stop-gate] 审计写入失败", exc_info=True)
        return False


def outbound_check(
    store: Any,
    *,
    path: str,
    platform: str,
    account_id: str = "",
    peer: str = "",
    conversation_id: str = "",
    phone: str = "",
    config: Any = None,
    do_audit: bool = True,
) -> str:
    """出站前查名单：已停联 → 返回来源码并（默认）记审计 ``blocked``；否则 ""。绝不抛。

    开关关闭（``compliance.stop_gate.enabled: false``）时仍查**会话冻结**（那是 O-1 A 既有硬停，
    不归本开关管），只是不做跨账号 / 手机号扩展。
    """
    try:
        if not enforced(config):
            if store is None:
                return ""
            from src.inbox.stop_contact import frozen_reason
            c = conversation_id or _conv_id(platform, account_id, peer)
            src = "frozen" if frozen_reason(store, c) == FREEZE_REASON else ""
        else:
            src = contact_stopped(store, platform, account_id, peer,
                                  conversation_id=conversation_id, phone=phone)
    except Exception:
        return ""
    if src and do_audit:
        audit(store, path=path, action="blocked", platform=platform, account_id=account_id,
              peer=peer, conversation_id=conversation_id, reason=STOP_REASON, hit=src)
    return src


def confirm_text(lang: Any = "") -> str:
    """唯一允许的那一条模板确认（复用 stop_contact.farewell_text，不走 LLM）。"""
    try:
        from src.inbox.stop_contact import farewell_text
        return farewell_text(lang)
    except Exception:
        return "Okay, I won't message you again."


def guess_lang(text: Any) -> str:
    """模板确认的语言粗判（只看文字系统）；默认 en。纯函数。

    他加禄 / 宿务不在这里改判：默认确认文案仍走 farewell（没有 tl 时回英文）。
    打开 ``multilingual_confirm`` 后由 :func:`confirm_lang` 再细分。
    """
    s = str(text or "")
    if re.search(r"[\u0900-\u097f]", s):
        return "hi"
    if re.search(r"[\u3400-\u9fff]", s):
        return "zh"
    return "en"


# 显式打开才用 snippets。缺省 / false / legacy / off 都保持今天的 farewell 路径。
_CONFIRM_ON = frozenset({"1", "true", "yes", "on", "enforce", "enabled"})
_SNIPPET_PATH = (
    Path(__file__).resolve().parents[2]
    / "config" / "presets" / "snippets" / "stop_confirm_templates.yaml"
)


def multilingual_confirm_enabled(config: Any = None) -> bool:
    """``compliance.stop_gate.multilingual_confirm`` 是否打开。默认关。"""
    value = gate_cfg(config).get("multilingual_confirm", False)
    if isinstance(value, bool):
        return value is True
    if isinstance(value, (int, float)):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in _CONFIRM_ON
    return False


def confirm_lang(text: Any) -> str:
    """打开多语确认后，这条 STOP 用哪种模板。

    天城文 → hi，含汉字 → zh，否则用 ``refine_latin_language``：
    variant ``ceb`` → ceb，lang ``tl`` → tl，其余 → en。
    """
    s = str(text or "")
    if re.search(r"[\u0900-\u097f]", s):
        return "hi"
    if re.search(r"[\u3400-\u9fff]", s):
        return "zh"
    try:
        from src.inbox.session_lang import refine_latin_language
        lang, variant = refine_latin_language(s, "en")
    except Exception:
        return "en"
    if variant == "ceb":
        return "ceb"
    if lang == "tl":
        return "tl"
    if not lang or lang in ("en", "unknown"):
        return "en"
    return str(lang)


def _farewell_covers(lang: str) -> bool:
    """farewell 表里已经有这门语言时，继续用那句，不用 snippets（en/zh 文案不同）。"""
    try:
        from src.inbox.stop_contact import farewell_languages
        key = str(lang or "").strip().lower().replace("_", "-")
        return key in set(farewell_languages())
    except Exception:
        return lang in ("en", "zh")


def _snippet_persona(lang: str) -> str:
    try:
        import yaml
        if not _SNIPPET_PATH.is_file():
            return ""
        data = yaml.safe_load(_SNIPPET_PATH.read_text(encoding="utf-8")) or {}
        node = (data.get("templates") or {}).get(lang) or {}
        if isinstance(node, dict):
            return str(node.get("persona") or "").strip()
    except Exception:
        return ""
    return ""


def resolve_confirm_text(text: Any = "", config: Any = None) -> str:
    """STOP 确认句。开关关闭时与 ``confirm_text(guess_lang(text))`` 相同。

    开关打开后，farewell 已覆盖的语言（含 en / zh）仍用 farewell；
    tl / ceb / hi 用 snippets 的 persona 句。Taglish 被判成 tl，走 tl 那句。
    文件缺失则回退 farewell。
    """
    if not multilingual_confirm_enabled(config):
        return confirm_text(guess_lang(text))
    lang = confirm_lang(text)
    if _farewell_covers(lang):
        return confirm_text(lang)
    snippet = _snippet_persona(lang)
    if snippet:
        return snippet
    return confirm_text(lang)


__all__ = [
    "STOP_REASON", "FREEZE_REASON", "LOCKED_BY", "normalize", "match_lexicon", "detect",
    "is_stop_message", "gate_cfg", "enforced", "phone_key", "contact_stopped", "record_stop",
    "audit", "outbound_check", "confirm_text", "guess_lang",
    "multilingual_confirm_enabled", "confirm_lang", "resolve_confirm_text",
]
