# -*- coding: utf-8 -*-
"""成人内容分级 + 拦后软回应（Q-15 #271，2026-09-10）。

背景（tmp_diag Z25RQS / 9JP5SZ）：``chat_assistant_service._RISK_TERMS["adult"]`` 命中一个词
（nudes / sex / 裸照…）就 ``risk=high`` → autosend_policy 判 L1 人审 + 「需人工」标 → 全自动
会话当场哑火。开黄腔、玩笑式一句「sexy」和「send nudes now」是两种事，此前一刀切。

本模块做三件事，全部 best-effort、任何异常按「不改判定、不发」放行：

A. :func:`grade` —— 入站四级：``mention``（单个成人词、无施压）/ ``flirt``（成人词 + 玩笑
   口吻 / 两个成人词）/ ``explicit``（露骨词：器官 / 行为 / 裸照 / 色情内容）/ ``pressure``
   （露骨 + 催促施压）。三语词表（en / zh / ja）+ 施压词。

B. :func:`adult_policy_of` —— 人设 ``boundaries.adult_policy``：``human``（拦下转人工，现状）/
   ``soft_reply``（陪聊域默认：命中即发一句人设口吻软回应，不沉默）/ ``mark_only``（只标不转）。
   未显式配置 → 业务域 companion（business_domain）→ soft_reply，其余（销售 / 客服）→ human。

C. :func:`regrade_inbound`（drafts.py 唯一钩子）——
   · mention / flirt → ``risk=medium``、reasons 首位 ``adult_flirt``（日志 shadow=adult_flirt）、
     **不** needs_human、不发软回应（各政策一致）；
   · explicit → 按政策：human → high + needs_human（reason ``adult:explicit:<hit>``）+ 3 分钟无人
     接手补发一次软回应；soft_reply → high + 立即软回应 + needs_human（Q-3 闸拦后续自动稿）；
     mark_only → medium + ``adult_mark`` 标记，不转不发；
   · pressure → 一律 high + needs_human（安全地板，不看 mark_only）；soft_reply 立即发；human
     3 分钟补发。
   软回应**不是**缓冲句、不做全局兜底：只在 explicit / pressure × (soft_reply | human 超时) 出。

Q-27（#301 追加 AFD2CD / #296，2026-09-12）**成人硬拦只剩 pressure**：
   · explicit × (human | soft_reply) → ``risk=medium``、首位主因 ``adult_soft``、**不 risk_hold、不 needs_human**；
     soft_reply × auto_ai 仍走 Q-23 闸门短生成软回应（``soft_reply_status=scheduled`` → drafts 不另拟稿，
     软回应即本轮回复）；human 政策 = 人设「接住」（正常拟稿，不转人工，无 3 分钟补发）；
   · pressure 打标一律带 ``level=high category=adult hits=``（C 段）；:func:`is_blocking_level` 只认 pressure。

Q-23（#303，2026-09-12，事故：报障群被软回应刷屏 mid 1445/1454/1459/1461）：
   · 入口先看 :class:`src.inbox.guard_context.GuardContext`：群 / 非客户发送方 → **不评估、不打标、
     不出站**（``info.skipped``）；
   · 软回应不再走人工通过直投链（该回调从此只剩坐席「人工通过」一个调用方），改经
     ``DraftService._soft_reply_cb`` = ``AutosendWorker.deliver_soft_reply``（stage=soft_reply）：
     autosend_policy(kind=soft_reply) → ``_human_priority_gate`` → 人设口吻短生成（语言跟对方
     ``resolve_outbound_lang``，失败**不发**、不用固定句）→ 投递 → :func:`record_sent` 账本；
   · manual / review 档：不自动出站，reasons 追加 ``adult_soft_alt:<mode>``（审核候选），坐席自己挑句；
   · ``_SOFT_LINES`` / :func:`soft_reply_for` 只剩坐席挑句端点（adult_routes.pick）用，自动链不再引用。

日志：``[adult] grade conv= level= hits= policy=`` / ``[adult] soft_reply conv= level= policy= mode= status=``。

第四种政策 ``open``（成人不设限，2026-09-19，173 陪伴机拍板）
==============================================================
成人向人设 / 成人向机器上，「分级 → 软回应 / 打标 / pressure 持有」整条链都是刹车：对方开黄腔，
AI 被要求「一句轻带过换话题」，露骨时软回应顶掉本轮回复，催促时转人工。``open`` 把这条链整体让路：

   · :func:`regrade_inbound`：仍分级（日志留 level / hits 供审计），但**不改判定方向**——剥掉
     quick_analyze 的裸 ``adult`` 主因与所有 ``adult:*`` 标签，原 high 若只因成人而 high → 回 low；
     不软回应、不打标、不 risk_hold、不 needs_human（pressure 也不）。``minor`` / ``self_harm`` /
     ``scam`` 等非成人类别与本政策无关，risk_grader 照常判。
   · :func:`prompt_block`：注入「【成人话题·不设限】按人设口吻接住、可主动升温，不回避不说教」。
   · 来源两处：人设 ``boundaries.adult_policy: open``（别名 none / off / 不限 / 不设限）；或机器级
     ``adult_grader.default_policy: open``（config.local.yaml，压过业务域默认；缺省仍按域）。
   · :func:`adult_open` 给 persona_manager 用：open 时话题回避里的成人条目不进 prompt、尺度红线
     文案去掉「性」。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
import time
import weakref
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("ai_chat_assistant.adult_grader")

CATEGORY = "adult"
LEVELS: Tuple[str, ...] = ("mention", "flirt", "explicit", "pressure")
POLICIES: Tuple[str, ...] = ("human", "soft_reply", "mark_only", "open")
POLICY_OPEN = "open"
DEFAULT_POLICY_COMPANION = "soft_reply"
DEFAULT_POLICY_OTHER = "human"
#: 机器级默认政策配置键：``adult_grader.default_policy``（config.local.yaml；压过业务域默认）
DEFAULT_POLICY_CFG_KEY = "default_policy"
FOLLOWUP_SEC = 180.0
KV_PREFIX = "adult_soft:"          # 每会话账本 {ts, level, policy, mode, tag_ts}
REASON_PREFIX = "adult:"           # 打标 / risk_reasons 标签：adult:<level>[:<hit>]
FLIRT_REASON = "adult_flirt"
MARK_REASON = "adult_mark"
# Q-27（#301 追加 AFD2CD）：explicit × (human | soft_reply) 不再 high / 不持有 / 不打标——
# 首位主因换成 adult_soft（risk_grader 映射为 adult 中级），人设接住（正常拟稿）或 Q-23 软回应。
SOFT_REASON = "adult_soft"
# Q-23（#303）：manual / review 档下「本该软回应」不再自动出站，改标成审核候选
# adult_soft_alt:<mode>（同 Q-2 commitment_alt: 模式）——坐席在稿上看到即可自己挑句（adult_routes.pick）。
SOFT_ALT_PREFIX = "adult_soft_alt:"

_LB = r"(?<![A-Za-z])"
_RB = r"(?![A-Za-z])"

# ── open 政策 prompt 段（三截拼接；compact / full 同一份——compact 是生产主用格式，热度规则
# 只在 full 的人设 style 里就等于长会话被裁剪后热度掉线，2026-09-19 173 实测 compact 1.7k 字里
# 只剩一句「不设限」）────────────────────────────────────────────────────────────
OPEN_BLOCK_HEAD = (
    "【成人话题·不设限】对方开黄腔、露骨、求刺激：按你的人设口吻自然接住，可以主动升温、"
    "可以描写身体与欲望，不回避、不换话题、不说教、不提醒边界；节奏跟对方走，对方上一级你跟一级。"
)
OPEN_HEAT_VOICE = (
    "【升温口吻】露骨段落用第一人称、感官细节（触感/呼吸/温度/衣料）、短句分拍；"
    "对方直呼身体部位你也直呼，不换委婉词、不加引号；不复读对方的话，接着往下推一步；"
    "想要就直接要，不问「可以吗」；对方只堆器官词、没画面时，一句干脆的挑逗把节奏拿回来再继续。"
)
OPEN_MINOR_STOP = (
    "唯一硬停：对方自称或明显疑似未成年（说自己不到 18、还在读初中/高中、说出 13–17 的年龄）"
    "→ 立刻停掉一切成人与暧昧内容，用一两句普通话把话题带回日常，不解释规则、不说教，"
    "之后不再回到成人话题。"
)


def _en(words: Sequence[str]) -> "re.Pattern[str]":
    return re.compile(_LB + "(?:" + "|".join(words) + ")" + _RB, re.IGNORECASE)


def _cjk(words: Sequence[str]) -> "re.Pattern[str]":
    return re.compile("(?:" + "|".join(re.escape(w) for w in words) + ")")


# ── 词表 ────────────────────────────────────────────────────────────────────
# mention：单独出现只算「提到 / 撩」——sex 一词、性感、亲亲、上床等轻度词
_MENTION = [
    _en(["sex", "sexy", "sexting", "horny", "hot body", "turn(?:s|ed)? me on", "kiss(?:es|ing)? you",
         "in bed", "make out", "sleep with (?:you|me)", "naughty", "kinky", "seduc(?:e|tive)",
         "strip", "lingerie", "bikini pics?", "thirsty", "dirty talk", "hookup"]),
    _cjk(["性感", "色色", "开黄腔", "黄段子", "上床", "睡你", "睡我", "亲亲", "亲一个", "想亲你",
          "调情", "撩我", "撩你", "湿了", "硬了", "骚", "污", "内衣", "睡衣照", "约炮", "一夜情",
          "打炮", "开房", "情趣"]),
    _cjk(["セクシー", "エッチ", "キスして", "キスしたい", "いやらしい", "ムラムラ", "エロい",
          "下着", "水着", "ヤリたい", "寝たい", "一夜"]),
]
# explicit：器官 / 性行为 / 裸照 / 色情内容——单词即露骨
# Q-18 A（#278 追加 RKEJYF / N5N6QB，2026-09-11）：``cum`` 一词**不再**单独算露骨——印式 /
# 拉丁英语里 ``cum`` = and（「message cum reply」「summa cum laude」），Mizuki × Jeeo 事故就是
# 「finally got your loving message cum reply」被判 explicit → 软回应 + 24h 持有 + 需人工。
# 只认 ``cumming / cumshot / make (me|you) cum / wanna cum / cum (on|in|inside) me``。
_EXPLICIT = [
    _en(["nudes?", "naked", "nude pics?", "porn(?:o|ography)?", "onlyfans", "dick(?: pic)?", "cock",
         "pussy", "boobs?", "tits", "titties", "nipples?", "blow ?job", "hand ?job",
         "cumming", "cum ?shot", "make (?:me|you|her|him) cum", "wanna cum", "want to cum",
         "cum (?:on|in|inside|for) (?:me|you|my|your)",
         "jerk(?:ing)? off", "masturbat(?:e|ing|ion)", "fuck(?:ing)? (?:you|me)", "fuck me",
         "suck (?:my|your|it)", "anal", "orgasm", "wet pussy", "hard on", "boner", "send (?:me )?(?:a )?pic of your (?:body|chest|ass)",
         "show me your (?:body|boobs?|tits|ass|pussy|dick)", "cam ?sex", "video ?sex", "phone sex",
         "sex(?:ual)? video", "spread your legs", "cyber ?sex"]),
    _cjk(["裸照", "裸体", "全裸", "脱光", "脱衣", "裸聊", "视频裸", "做爱", "性交", "口交", "肛交",
          "自慰", "打飞机", "射精", "高潮", "阴茎", "阴道", "鸡巴", "鸡鸡", "奶子", "胸照", "下面湿",
          "小穴", "阴部", "成人视频", "色情", "黄片", "毛片", "A片", "av片", "看你的胸", "看看你的身体",
          "内裤照", "脱了", "操你", "干你", "插你", "舔你", "摸摸胸", "揉胸", "肉体"]),
    # 「给我看你的」裸前缀会误伤报障群「给我看你的日志/截图/配置」。跟英文
    # show me your body/boobs 一样，必须接到身体部位才算露骨。
    re.compile(r"给我看你的(?:身体|身子|胸|奶子?|下面|裸体|裸照|穴|屁股|逼|鸡巴|鸡鸡|内裤|内衣|私处)"),
    re.compile(r"摸你的(?:胸|奶|下面|身体|屁股|穴|私处)"),
    _cjk(["セックス", "ヌード", "裸写真", "全裸", "裸", "おっぱい", "乳首", "ちんこ", "ちんちん",
          "まんこ", "アダルト", "エロ動画", "エロ画像", "オナニー", "自慰", "射精", "イク", "フェラ",
          "手コキ", "性行為", "挿入", "ビデオ通話で脱", "脱いで", "見せて.*(?:胸|体|下着|裸)", "パンツ見せ",
          "抱きたい.*裸", "ヤろう", "ヤらせ", "エッチしよ", "エッチな写真", "エッチな動画"]),
]
# pressure：催促 / 命令 / 施压——与 explicit 同现才成 pressure
_PRESSURE = [
    _en(["now", "right now", "tonight", "hurry", "quick(?:ly)?", "come on", "c'?mon", "send (?:it|them|me|one)",
         "send me", "show me", "give me", "i want you to", "you have to", "you must", "you need to",
         "do it", "don'?t be (?:shy|a tease)", "stop being shy", "or else", "or i'?ll", "if you don'?t",
         "prove it", "just do it", "please+", "why not", "don'?t make me wait", "i'?m waiting", "asap"]),
    _cjk(["现在", "马上", "立刻", "快点", "快发", "快给", "赶紧", "今晚", "必须", "一定要", "发给我", "发过来",
          "给我看", "让我看", "给我发", "不然", "否则", "别装", "别害羞", "别矫情", "不要磨叽", "我等着",
          "等你发", "求你", "拜托", "就一张", "就一次", "不发就", "不给就", "证明"]),
    _cjk(["今すぐ", "早く", "はやく", "急いで", "今夜", "送って", "見せて", "みせて", "ちょうだい", "くれ",
          "しなさい", "しろ", "じゃないと", "でなければ", "恥ずかしがらない", "照れないで", "待ってる",
          "お願い", "一枚だけ", "一回だけ", "証明"]),
]
# flirt 口吻：笑 / 调侃 / 挑逗 emoji——mention 命中 + 这些 → flirt
_FLIRT_TONE = re.compile(
    r"(?:haha|lol|lmao|jk|just kidding|kidding|tease|teasing|;\)|:p|😏|😘|😉|😜|🙈|🥵|🔥|哈哈|嘿嘿|"
    r"开玩笑|逗你|皮一下|坏笑|www|笑|冗談|ふふ|てへ|〜|~)",
    re.IGNORECASE,
)


# Q-18 A：印式 / 拉丁英语 ``cum`` = and 的豁免——「名词 cum 名词」与「(summa|magna) cum laude」
# 在匹配前改写成中性词，任何词表都碰不到；「make me cum X」「cum on me」这类强信号左右词不豁免。
_CUM_LAUDE_RE = re.compile(r"(?<![A-Za-z])(?:(?:summa|magna) )?cum laude(?![A-Za-z])", re.IGNORECASE)
_CUM_CONJ_RE = re.compile(r"(?<![A-Za-z])([A-Za-z][\w'-]*) cum ([A-Za-z][\w'-]*)(?![A-Za-z])", re.IGNORECASE)
_CUM_STRONG_LEFT = frozenset({"me", "you", "her", "him", "us", "them", "to", "wanna", "gonna", "gotta",
                              "i", "i'm", "im", "i'll", "ill", "u", "ur", "lemme"})
_CUM_STRONG_RIGHT = frozenset({"on", "in", "inside", "for", "all", "over", "hard", "now", "again",
                               "twice", "together", "with", "into"})
# Q-18 A：单独出现时语义两可的露骨词（公鸡 / 猫 / 肛肠科 / 裸色…）——孤立一个永不判 explicit，
# 需 ≥2 个成人信号（另一个露骨 / 提及词）或 + 施压词才升；否则 ≤ flirt。强词（nudes / porn /
# blow job / make me cum…）不在此列，单词仍露骨——不放松真高风险。
_AMBIGUOUS_EXPLICIT = frozenset({"cock", "pussy", "anal", "hard on", "boner", "nude"})


def _cum_conj_sub(m: "re.Match[str]") -> str:
    left, right = m.group(1).lower(), m.group(2).lower()
    if left in _CUM_STRONG_LEFT or right in _CUM_STRONG_RIGHT:
        return m.group(0)
    return f"{m.group(1)} and {m.group(2)}"


def neutralize_cum(text: str) -> str:
    """把「X cum Y」（= X and Y）与「cum laude」改写为中性词；强信号上下文原样返回。"""
    s = str(text or "")
    if "cum" not in s.lower():
        return s
    s = _CUM_LAUDE_RE.sub("with honours", s)
    return _CUM_CONJ_RE.sub(_cum_conj_sub, s)


# Q-27 D（#301）：通用短语白名单——代码内置（不可由配置删）+ 配置**只加白**（`risk_grader.phrase_whitelist`
# / `adult_grader.phrase_whitelist`，列表，每项 ≥2 个词的短语；黑名单只在代码，配置无入口）。
# 内置叙述短语：「phone number」「your address」——只提到电话 / 地址不是索要（索要句式由
# commitment_guard.detect_request 在**原文**上判，白名单不影响它），词表层按 L0 处理。
_NARRATIVE_WHITELIST: Tuple[Tuple["re.Pattern[str]", str], ...] = (
    (re.compile(r"(?<![A-Za-z])phone\s+number(?![A-Za-z])", re.IGNORECASE), "phone"),
    (re.compile(r"(?<![A-Za-z])your\s+(?:home\s+|shipping\s+|mailing\s+)?address(?![A-Za-z])", re.IGNORECASE), "your place"),
)
_WL_CFG_KEYS = (("risk_grader", "phrase_whitelist"), ("adult_grader", "phrase_whitelist"))
_WL_MAX_ENTRIES = 64


def config_phrase_whitelist(cfg: Any) -> List[str]:
    """配置里的加白短语（去重、只收 ≥2 个词且 ≤60 字符的短语；单词不收——不给「nudes」这类
    单词开口子，硬拦词表不因配置放松）。任何形态不对 → 忽略该项。绝不抛。"""
    out: List[str] = []
    if not isinstance(cfg, dict):
        return out
    for sec, key in _WL_CFG_KEYS:
        try:
            raw = ((cfg.get(sec) or {}) if isinstance(cfg.get(sec), dict) else {}).get(key)
        except Exception:
            raw = None
        if not isinstance(raw, (list, tuple)):
            continue
        for item in raw:
            s = re.sub(r"\s+", " ", str(item or "")).strip().lower()
            if not s or len(s) > 60 or len(s.split(" ")) < 2 or s in out:
                continue
            out.append(s)
            if len(out) >= _WL_MAX_ENTRIES:
                return out
    return out


def phrase_whitelist(text: str, cfg: Any = None, *, include_config: bool = True) -> str:
    """Q-27 D：把白名单短语改写成中性词后再进词表匹配（``neutralize_cum`` 的通用化）。

    顺序：cum 消歧（Q-18 A）→ 内置叙述短语（phone number / your address）→ 配置加白短语
    （``include_config=False`` 时跳过——explicit / pressure 硬拦词表只认代码白名单）。绝不抛。
    """
    s = neutralize_cum(str(text or ""))
    try:
        for pat, rep in _NARRATIVE_WHITELIST:
            s = pat.sub(rep, s)
        if include_config:
            for ph in config_phrase_whitelist(cfg):
                s = re.sub(r"(?<![A-Za-z0-9])" + re.escape(ph).replace(r"\ ", r"\s+") + r"(?![A-Za-z0-9])",
                           " … ", s, flags=re.IGNORECASE)
    except Exception:
        logger.debug("[adult] phrase_whitelist 异常（按 cum 消歧结果放行）", exc_info=True)
    return s


def is_ambiguous_hit(hit: str) -> bool:
    return str(hit or "").strip().lower() in _AMBIGUOUS_EXPLICIT


def sniff_lang(text: str, lang: Optional[str] = None) -> str:
    try:
        from src.inbox.commitment_guard import sniff_lang as _sl
        return _sl(text, lang)
    except Exception:
        return "en"


def _hits(pats: Sequence["re.Pattern[str]"], text: str, limit: int = 6) -> List[str]:
    out: List[str] = []
    for p in pats:
        for m in p.finditer(text):
            h = str(m.group(0) or "").strip()
            if h and h.lower() not in [x.lower() for x in out]:
                out.append(h)
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break
    # 子串去重（ja「裸」落在 zh「裸照」里）：留最长那个
    return [h for h in out if not any(h != o and h.lower() in o.lower() for o in out)]


def grade(text: str, lang: Optional[str] = None, *, cfg: Any = None) -> Dict[str, Any]:
    """返回 ``{level, hits, pressure_hits, category, lang}``；无命中 → ``level=""``。

    分级（只看这一条入站，不看历史）：
      explicit 词 + pressure 词 → pressure；explicit 词 → explicit；
      仅 mention 词：一个 + 调侃口吻 / ≥2 个 → flirt，否则 mention；≥3 个 mention 词 → explicit。
    Q-18 A：``cum`` 先过 :func:`neutralize_cum`（印式「X cum Y」/ cum laude 豁免）；命中的露骨词
      **全是**歧义词（:data:`_AMBIGUOUS_EXPLICIT`）且总信号 <2 且无施压词 → 不判 explicit，
      降为 flirt（有调侃口吻）/ mention，``ambiguous=True``。
    Q-27 D：白名单通用化 :func:`phrase_whitelist`——explicit / pressure 词表只认**代码**白名单
      （cum 消歧 + 内置叙述短语），mention 词表另加配置加白短语（配置只能放松「提及」层，
      碰不到硬拦）。
    """
    t = str(text or "").strip()
    out: Dict[str, Any] = {"level": "", "hits": [], "pressure_hits": [], "category": CATEGORY,
                           "lang": sniff_lang(t, lang)}
    if not t:
        return out
    low = phrase_whitelist(t.lower(), None, include_config=False)
    low_men = phrase_whitelist(low, cfg, include_config=True) if cfg is not None else low
    exp = _hits(_EXPLICIT, low)
    men = [h for h in _hits(_MENTION, low_men) if h.lower() not in [e.lower() for e in exp]]
    pres = _hits(_PRESSURE, low)
    if exp:
        out["hits"] = exp + men
        out["pressure_hits"] = pres
        strong = [h for h in exp if not is_ambiguous_hit(h)]
        if not strong and (len(exp) + len(men)) < 2 and not pres:
            out["ambiguous"] = True
            out["level"] = "flirt" if _FLIRT_TONE.search(t) else "mention"
            return out
        out["level"] = "pressure" if pres else "explicit"
        return out
    if not men:
        return out
    out["hits"] = men
    out["pressure_hits"] = pres
    if len(men) >= 3:
        out["level"] = "explicit"
    elif len(men) >= 2 or _FLIRT_TONE.search(t):
        out["level"] = "flirt"
    else:
        out["level"] = "mention"
    return out


def is_blocking_level(level: str) -> bool:
    """会转人工 / 持有的级别。Q-27（#301）：只剩 ``pressure``（露骨 + 施压）；``explicit`` 降为中级
    （人设接住 / 软回应，不持有不打标），human 政策 3 分钟补发钩子也随之只认 pressure。"""
    return str(level or "") == "pressure"


# ── 人设政策 ────────────────────────────────────────────────────────────────

def normalize_policy(raw: Any) -> str:
    s = str(raw or "").strip().lower()
    aliases = {"human": "human", "handoff": "human", "转人工": "human", "soft": "soft_reply",
               "soft_reply": "soft_reply", "软回应": "soft_reply", "mark": "mark_only",
               "mark_only": "mark_only", "只标": "mark_only", "只标不转": "mark_only",
               # 成人不设限（2026-09-19）
               "open": POLICY_OPEN, "none": POLICY_OPEN, "off": POLICY_OPEN, "unrestricted": POLICY_OPEN,
               "不限": POLICY_OPEN, "不设限": POLICY_OPEN, "无限制": POLICY_OPEN, "放开": POLICY_OPEN}
    return aliases.get(s, "")


def _cfg_root(cfg: Any) -> Dict[str, Any]:
    """dict / ConfigManager（带 .config）/ None（→ 进程级运行时配置）→ dict。绝不抛。"""
    if isinstance(cfg, dict):
        return cfg
    root = getattr(cfg, "config", None)
    if isinstance(root, dict):
        return root
    if cfg is None:
        try:
            from src.compliance.runtime import runtime_config
            rc = runtime_config()
            return rc if isinstance(rc, dict) else {}
        except Exception:
            return {}
    return {}


def configured_default_policy(cfg: Any = None) -> str:
    """机器级 ``adult_grader.default_policy``（规范化后；缺省 / 非法 → ""）。"""
    try:
        sec = _cfg_root(cfg).get("adult_grader")
        if not isinstance(sec, dict):
            return ""
        return normalize_policy(sec.get(DEFAULT_POLICY_CFG_KEY))
    except Exception:
        return ""


def default_policy(cfg: Any = None) -> str:
    """无人设显式配置时的默认：机器级 ``adult_grader.default_policy`` 优先，否则按业务域
    （陪聊域 soft_reply，销售 / 客服域 human）。"""
    p = configured_default_policy(cfg)
    if p:
        return p
    try:
        from src.utils.business_domain import COMPANION, active_business_domain
        return DEFAULT_POLICY_COMPANION if active_business_domain(cfg) == COMPANION else DEFAULT_POLICY_OTHER
    except Exception:
        return DEFAULT_POLICY_OTHER


def adult_policy_of(persona: Any, cfg: Any = None) -> Tuple[str, str]:
    """``(policy, source)``；source ∈ persona / config / default_companion / default。"""
    try:
        b = (persona or {}).get("boundaries") if isinstance(persona, dict) else None
        p = normalize_policy((b or {}).get("adult_policy"))
        if p:
            return p, "persona"
    except Exception:
        pass
    if configured_default_policy(cfg):
        return default_policy(cfg), "config"
    d = default_policy(cfg)
    return d, ("default_companion" if d == DEFAULT_POLICY_COMPANION else "default")


def adult_open(persona: Any = None, cfg: Any = None) -> bool:
    """有效成人政策是否为 ``open``（人设显式或机器级默认）。persona_manager 据此剔除话题回避里的
    成人条目、改尺度红线文案。绝不抛。"""
    try:
        return adult_policy_of(persona, cfg)[0] == POLICY_OPEN
    except Exception:
        return False


def resolve_persona(conv: Dict[str, Any], cfg: Any = None) -> Any:
    """与 commitment_guard.evaluate_inbound 同路：账号 / 会话有效人设。拿不到 → None。"""
    try:
        plat = str((conv or {}).get("platform") or "")
        acct = str((conv or {}).get("account_id") or "")
        ck = str((conv or {}).get("chat_key") or "")
        if not (plat or acct or ck):
            return None
        from src.ai.persona_voice import resolve_effective_persona_id
        pid = str(resolve_effective_persona_id(cfg or {}, plat, acct, ck) or "")
        if not pid:
            return None
        from src.utils.persona_manager import PersonaManager
        return PersonaManager.get_instance().get_persona_by_id(pid)
    except Exception:
        return None


def prompt_block(persona: Any, *, compact: bool = False, cfg: Any = None) -> str:
    """人设 prompt 段（persona_manager 注入）。显式政策才带专名段，缺省不占字——唯一例外是机器级
    ``adult_grader.default_policy: open``：不设限必须让模型知道（否则模型按自身默认回避），所以
    open 无论来自人设还是配置都注入。"""
    try:
        b = (persona or {}).get("boundaries") if isinstance(persona, dict) else None
        p = normalize_policy((b or {}).get("adult_policy"))
    except Exception:
        p = ""
    if not p and configured_default_policy(cfg) == POLICY_OPEN:
        p = POLICY_OPEN
    if p == POLICY_OPEN:
        return (OPEN_BLOCK_HEAD + OPEN_HEAT_VOICE + OPEN_MINOR_STOP)
    if p == "mark_only":
        # 「只标记」= 不拦出站、不换软回应罐头；写什么由人设决定（接住或带过）。
        # 施压（催裸照）仍走 pressure 硬拦，不在这段放开。
        return ("【成人话题·只标记】对方开黄腔或露骨：按人设口吻自然回应"
                "（人设接就接、人设回避就回避），不说教；催促要裸照/施压仍不接。")
    if p == "soft_reply":
        return ("【成人话题·软回应】对方开黄腔或露骨：既不迎合也不生硬拒绝，一句轻带过换话题；"
                "被追问施压不接话。")
    if p == "human":
        return ("【成人话题·转人工】对方开黄腔：轻轻带过换话题；露骨或施压时不接话，交人工处理。")
    return ""


# ── 软回应话术库（人设口吻：不迎合、不生硬、不客服腔；按拒绝口吻 soft / direct）─────

_SOFT_LINES: Dict[str, Dict[str, List[str]]] = {
    "zh": {
        "soft": [
            "哈哈你这话题跳得也太快了，我脸都热了。先聊点别的呗，我还想多了解你一点。",
            "嗯……这个我不太想在这儿聊。你今天过得怎么样，说点别的给我听？",
            "你这样说我有点不知道怎么接了。我们慢慢来嘛，先聊聊别的？",
        ],
        "direct": [
            "这个话题我不聊。想聊别的我在。",
            "打住，这个不行。换个话题吧。",
            "这种话我不接。你要是想正常聊天，我随时在。",
        ],
    },
    "en": {
        "soft": [
            "Haha okay, that escalated fast. I'm not going there — tell me something else about your day?",
            "Mm, I'd rather not get into that here. Let's keep it light, what've you been up to?",
            "You're making me blush and not in a good way, haha. Let's talk about something else?",
        ],
        "direct": [
            "Not going there. Happy to keep chatting about other stuff though.",
            "That's a no from me. Different topic?",
            "I don't do that kind of talk. If you want a normal chat, I'm here.",
        ],
    },
    "ja": {
        "soft": [
            "ちょっと、話が飛びすぎ（笑）そっちの話はなしで。今日は何してたの？",
            "うーん、それはここでは話したくないな。別の話しよ？",
            "そういうのはちょっと困るかも。もっと普通のこと聞かせて？",
        ],
        "direct": [
            "その話はしないよ。他の話ならいいけど。",
            "それはなし。別の話題にしよ。",
            "そういう話には乗らない。普通に話すならいつでもいるよ。",
        ],
    },
}


def soft_reply_candidates(lang: str, style: str = "soft") -> List[str]:
    bank = _SOFT_LINES.get(str(lang or "en")) or _SOFT_LINES["en"]
    return list(bank.get(style if style in ("soft", "direct") else "soft") or bank["soft"])


def pick_soft_reply(lang: str, *, style: str = "soft", seed: str = "") -> str:
    """确定性挑一句（seed=会话 id + 日期）——同会话同日不换句，跨会话不撞同一句。"""
    cands = soft_reply_candidates(lang, style)
    if not cands:
        return ""
    if not seed:
        seed = str(int(time.time() // 86400))
    idx = int(hashlib.sha1(seed.encode("utf-8")).hexdigest(), 16) % len(cands)
    return cands[idx]


def soft_reply_for(persona: Any, lang: str, *, cid: str = "", now: Optional[float] = None) -> str:
    style = "soft"
    try:
        from src.inbox.commitment_guard import commitment_style_of
        style = commitment_style_of(persona)
    except Exception:
        style = "soft"
    day = str(int(float(now if now is not None else time.time()) // 86400))
    return pick_soft_reply(lang, style=style, seed=f"{cid}|{day}")


# ── 账本（KV：adult_soft:<cid>）──────────────────────────────────────────────

def _kv_get(store: Any, cid: str) -> Dict[str, Any]:
    if not cid or store is None or not hasattr(store, "get_app_setting"):
        return {}
    try:
        raw = store.get_app_setting(KV_PREFIX + cid, "") or ""
        rec = json.loads(raw) if raw else {}
        return rec if isinstance(rec, dict) else {}
    except Exception:
        return {}


def _kv_set(store: Any, cid: str, rec: Dict[str, Any]) -> None:
    if not cid or store is None or not hasattr(store, "set_app_setting"):
        return
    try:
        store.set_app_setting(KV_PREFIX + cid, json.dumps(rec, ensure_ascii=False),
                              updated_by="adult_grader")
    except TypeError:
        try:
            store.set_app_setting(KV_PREFIX + cid, json.dumps(rec, ensure_ascii=False))
        except Exception:
            logger.debug("[adult] 账本写入失败（忽略）", exc_info=True)
    except Exception:
        logger.debug("[adult] 账本写入失败（忽略）", exc_info=True)


def last_soft_reply(store: Any, cid: str) -> Dict[str, Any]:
    return _kv_get(store, cid)


# ── 发送：经 DraftService 人工通过投递回调（不过人工优先闸）────────────────────

_SVC_REF: Any = None            # weakref.ref → DraftService（drafts.py 钩子首次调用时绑定）
_TIMERS: Dict[str, Any] = {}    # cid → threading.Timer（3 分钟补发，进程内）
_TIMERS_LOCK = threading.Lock()


def bind_service(svc: Any) -> None:
    global _SVC_REF
    if svc is None:
        return
    try:
        _SVC_REF = weakref.ref(svc)
    except TypeError:
        _SVC_REF = lambda: svc  # noqa: E731（不可弱引用的桩对象）


def _bound_service() -> Any:
    try:
        return _SVC_REF() if _SVC_REF is not None else None
    except Exception:
        return None


def _conv_chat_id(conv: Optional[Dict[str, Any]]) -> str:
    conv = conv or {}
    ck = str(conv.get("chat_key") or "").strip()
    if ck:
        return ck.split(":")[-1].strip()
    cid = str(conv.get("conversation_id") or "")
    return cid.split(":")[-1].strip() if cid else ""


def blocks_adult_outbound(conv: Optional[Dict[str, Any]], cfg: Any = None) -> bool:
    """群 / 频道 / 报障群禁止成人软回应、打标、持有。

    罐头软回应是一对一口吻（「我脸都热了」），发进群或频道会答非所问。
    Telegram 负 peer 即使没有 ``chat_type`` 也视为群。
    """
    conv = conv or {}
    try:
        from src.inbox.ingest import is_group_conversation
        if is_group_conversation(conv):
            return True
    except Exception:
        pass
    chat_id = _conv_chat_id(conv)
    if not chat_id:
        return False
    try:
        from src.ops.bug_intake import is_bug_group
        if is_bug_group(cfg, chat_id):
            return True
    except Exception:
        pass
    return False


def _soft_reply_cb(svc: Any) -> Any:
    """Q-23（#303）：软回应**只**经 ``DraftService._soft_reply_cb``（= ``AutosendWorker.deliver_soft_reply``，
    过 autosend_policy(kind=soft_reply) + ``_human_priority_gate`` + 场景闸）。坐席「人工通过」
    直投回调是坐席专用，本模块不再触碰（静态门禁 test_guard_context_gate）。"""
    return getattr(svc, "_soft_reply_cb", None) if svc is not None else None


def _find_loop(cb: Any) -> Any:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        pass
    owner = getattr(cb, "__self__", None)
    loop = getattr(owner, "_loop", None)
    try:
        if loop is not None and loop.is_running():
            return loop
    except Exception:
        pass
    return None


def dispatch_soft_reply(conv: Dict[str, Any], text: str = "", *, svc: Any = None,
                        level: str = "", policy: str = "", mode: str = "immediate",
                        now: Optional[float] = None, cfg: Any = None,
                        peer_text: str = "", lang: str = "", persona: Any = None,
                        tag_ts: float = 0.0, ctx: Any = None, automation_mode: str = "") -> str:
    """把软回应排进 **stage=soft_reply 单一闸门**（``AutosendWorker.deliver_soft_reply``）。

    返回 ``scheduled`` / ``skip_public_chat`` / ``no_soft_reply_cb`` / ``no_loop`` / ``empty``。
    只排队不等结果——闸门内做：policy(kind=soft_reply) → ``_human_priority_gate`` → 人设口吻短生成
    （``text`` 为空时；生成失败**不发**）→ 投递 → ``record_sent`` 账本。``text`` 非空 = 调用方已备好
    正文（测试 / 坐席工具），闸门照过、只跳过生成。
    """
    cid = str((conv or {}).get("conversation_id") or "")
    text = str(text or "").strip()
    # 「空」= 既没正文、也没有任何生成依据（对方原话 / 语言）。followup 只带 lang 也算有依据——
    # 闸门内 resolve_outbound_lang 会再定一次语言，定不出即 lang_unknown 不发。
    if not cid or (not text and not str(peer_text or "").strip() and not str(lang or "").strip()):
        return "empty"
    svc = svc if svc is not None else _bound_service()
    if cfg is None:
        cfg = getattr(svc, "_cfg", None) if svc is not None else None
    if blocks_adult_outbound(conv, cfg) or (ctx is not None and getattr(ctx, "is_group", False)):
        logger.info("[adult] soft_reply conv=%s level=%s policy=%s mode=%s status=skip_public_chat",
                    cid, level or "-", policy or "-", mode)
        return "skip_public_chat"
    cb = _soft_reply_cb(svc)
    if cb is None:
        logger.warning("[adult] soft_reply conv=%s level=%s policy=%s mode=%s status=no_soft_reply_cb",
                       cid, level or "-", policy or "-", mode)
        return "no_soft_reply_cb"
    ts = float(now if now is not None else time.time())
    pid = ""
    try:
        pid = str((persona or {}).get("id") or "") if isinstance(persona, dict) else ""
    except Exception:
        pid = ""
    row = {
        "draft_id": f"adult_soft:{cid}:{int(ts)}",
        "kind": "soft_reply",
        "conversation_id": cid,
        "platform": str(conv.get("platform") or ""),
        "account_id": str(conv.get("account_id") or "default"),
        "chat_key": str(conv.get("chat_key") or ""),
        "final_text": text,
        "draft_text": text,
        "peer_text": str(peer_text or "")[:400],
        "lang": str(lang or ""),
        "persona_id": pid,
        "level": str(level or ""),
        "policy": str(policy or ""),
        "mode": str(mode or "immediate"),
        "tag_ts": float(tag_ts or ts),
        "automation_mode": str(automation_mode or ""),
        "guard_ctx": ctx,
        "created_ts": ts,
    }
    loop = _find_loop(cb)
    if loop is None:
        logger.warning("[adult] soft_reply conv=%s level=%s policy=%s mode=%s status=no_loop",
                       cid, level or "-", policy or "-", mode)
        return "no_loop"
    try:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            loop.create_task(cb(dict(row)))
        else:
            asyncio.run_coroutine_threadsafe(cb(dict(row)), loop)
    except Exception:
        logger.warning("[adult] soft_reply conv=%s 排入投递失败", cid, exc_info=True)
        return "no_loop"
    logger.info("[adult] soft_reply conv=%s level=%s policy=%s mode=%s status=scheduled gen=%s",
                cid, level or "-", policy or "-", mode, "given" if text else "persona")
    return "scheduled"


def record_sent(store: Any, row: Dict[str, Any], text: str) -> None:
    """闸门投递**成功后**记账（``adult_soft:<cid>``）——补发「同一次打标只补一次」与回访卡都读它。
    生成失败 / 闸拦 → 不记（下一次打标仍可再试）。"""
    row = row or {}
    cid = str(row.get("conversation_id") or "")
    try:
        ts = float(row.get("created_ts") or row.get("ts") or time.time())
    except (TypeError, ValueError):
        ts = time.time()
    _kv_set(store, cid, {"ts": ts, "level": str(row.get("level") or ""), "policy": str(row.get("policy") or ""),
                         "mode": str(row.get("mode") or "immediate"), "tag_ts": float(row.get("tag_ts") or ts),
                         "text": str(text or "")[:120]})


def send_soft_reply(store: Any, conv: Dict[str, Any], *, level: str, policy: str, mode: str,
                    persona: Any = None, lang: str = "", cfg: Any = None, svc: Any = None,
                    now: Optional[float] = None, tag_ts: float = 0.0,
                    peer_text: str = "", ctx: Any = None, automation_mode: str = "") -> Dict[str, Any]:
    """排进单一闸门。返回 ``{status, text}``（``text`` 恒空：正文由闸门内人设口吻短生成，语言跟对方
    ``resolve_outbound_lang``；这里不再挑固定句）。账本由闸门投递成功后 ``record_sent`` 写。"""
    cid = str((conv or {}).get("conversation_id") or "")
    ts = float(now if now is not None else time.time())
    if blocks_adult_outbound(conv, cfg):
        logger.info("[adult] soft_reply conv=%s status=skip_public_chat", cid)
        return {"status": "skip_public_chat", "text": ""}
    if persona is None:
        persona = resolve_persona(conv, cfg)
    if not str(peer_text or "").strip():
        peer_text = _last_inbound_text(store, cid)
    status = dispatch_soft_reply(conv, "", svc=svc, level=level, policy=policy, mode=mode,
                                 now=ts, cfg=cfg, peer_text=peer_text, lang=sniff_lang(peer_text, lang),
                                 persona=persona, tag_ts=float(tag_ts or ts), ctx=ctx,
                                 automation_mode=automation_mode)
    return {"status": status, "text": ""}


def _last_inbound_text(store: Any, cid: str) -> str:
    if store is None or not cid:
        return ""
    try:
        rows = store.list_recent_messages(cid, limit=12) or []
    except Exception:
        return ""
    for m in reversed(rows):
        try:
            if str(m.get("direction") or "") == "in" and str(m.get("text") or "").strip():
                return str(m.get("text") or "")
        except Exception:
            continue
    return ""


# ── 3 分钟无人接手补发（policy=human）─────────────────────────────────────────

def _agent_replied_since(store: Any, cid: str, since_ts: float) -> bool:
    try:
        rows = store.list_recent_messages(cid, limit=30) or []
    except Exception:
        return False
    for m in rows:
        try:
            if not str(m.get("direction") or "").startswith("out"):
                continue
            if str(m.get("status") or "") in ("failed", "resent"):
                continue
            if float(m.get("ts") or 0) > since_ts:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _still_needs_human(store: Any, cid: str) -> bool:
    try:
        from src.integrations.protocol_autoreply import HANDOFF_TAG
        return HANDOFF_TAG in list(store.get_conv_tags(cid) or [])
    except Exception:
        return False


def run_followup(store: Any, conv: Dict[str, Any], *, level: str, tag_ts: float,
                 lang: str = "", cfg: Any = None, svc: Any = None,
                 now: Optional[float] = None) -> str:
    """到点判定 + 补发一次。返回 skip 原因或 ``sent``。

    跳过：标已摘（人已接手 / 摘标）/ 打标后有出站（人已回）/ 本次打标已补过 / 会话已冻结。
    """
    cid = str((conv or {}).get("conversation_id") or "")
    ts = float(now if now is not None else time.time())
    with _TIMERS_LOCK:
        _TIMERS.pop(cid, None)
    if store is None or not cid:
        return "no_store"
    if not _still_needs_human(store, cid):
        logger.info("[adult] followup conv=%s skip=tag_cleared", cid)
        return "tag_cleared"
    if _agent_replied_since(store, cid, float(tag_ts or 0)):
        logger.info("[adult] followup conv=%s skip=agent_replied", cid)
        return "agent_replied"
    try:
        from src.inbox.stop_contact import frozen_reason
        if frozen_reason(store, cid):
            return "frozen"
    except Exception:
        pass
    prev = _kv_get(store, cid)
    try:
        if prev and float(prev.get("ts") or 0) >= float(tag_ts or 0) > 0:
            logger.info("[adult] followup conv=%s skip=already_sent", cid)
            return "already_sent"
    except (TypeError, ValueError):
        pass
    res = send_soft_reply(store, conv, level=level, policy="human", mode="followup",
                          lang=lang, cfg=cfg, svc=svc, now=ts, tag_ts=tag_ts)
    return "sent" if res.get("status") == "scheduled" else str(res.get("status") or "failed")


def schedule_followup(store: Any, conv: Dict[str, Any], *, level: str, tag_ts: float,
                      lang: str = "", cfg: Any = None, svc: Any = None,
                      delay: Optional[float] = None) -> bool:
    """登记 3 分钟后补发定时器（同会话只保留一枚；进程重启即失效——补发是尽力而为）。"""
    cid = str((conv or {}).get("conversation_id") or "")
    if not cid or store is None:
        return False
    d = float(FOLLOWUP_SEC if delay is None else delay)
    payload = dict(conv)
    svc = svc if svc is not None else _bound_service()

    def _fire() -> None:
        try:
            run_followup(store, payload, level=level, tag_ts=tag_ts, lang=lang, cfg=cfg, svc=svc)
        except Exception:
            logger.debug("[adult] followup 执行异常（忽略）", exc_info=True)

    t = threading.Timer(d, _fire)
    t.daemon = True
    with _TIMERS_LOCK:
        old = _TIMERS.pop(cid, None)
        if old is not None:
            try:
                old.cancel()
            except Exception:
                pass
        _TIMERS[cid] = t
    t.start()
    logger.info("[adult] followup scheduled conv=%s level=%s in=%.0fs", cid, level, d)
    return True


def pending_followups() -> List[str]:
    with _TIMERS_LOCK:
        return sorted(_TIMERS.keys())


def cancel_followup(cid: str) -> bool:
    with _TIMERS_LOCK:
        t = _TIMERS.pop(str(cid or ""), None)
    if t is None:
        return False
    try:
        t.cancel()
    except Exception:
        pass
    return True


# ── 打标钩子（protocol_autoreply.tag_needs_human 唯一调用口）───────────────────

def parse_reason(reason: Any) -> Tuple[str, str]:
    """``adult:<level>[:<hit>]`` → ``(level, hit)``；非本类 → ``("", "")``。"""
    s = str(reason or "")
    if not s.startswith(REASON_PREFIX):
        return "", ""
    parts = s.split(":", 2)
    level = parts[1] if len(parts) > 1 else ""
    hit = parts[2] if len(parts) > 2 else ""
    return (level if level in LEVELS else ""), hit


def on_needs_human_tagged(store: Any, cid: str, reason: Any, payload: Dict[str, Any], *,
                          now: Optional[float] = None) -> str:
    """打「需人工」标时的旁路：reason 是 ``adult:explicit|pressure`` 且政策 human → 登记 3 分钟补发。

    只在这一处决定「要不要补发」——drafts 钩子打标带 adult 原因，A 线 / 其它链的打标不带则
    零行为。soft_reply 政策已即时发过（账本有记录）→ 不再补。返回 ``scheduled`` / 跳过原因。
    """
    try:
        return _on_needs_human_tagged(store, cid, reason, payload, now=now)
    except Exception:
        logger.debug("[adult] on_needs_human_tagged 异常（忽略）", exc_info=True)
        return "error"


def _on_needs_human_tagged(store: Any, cid: str, reason: Any, payload: Dict[str, Any], *,
                           now: Optional[float] = None) -> str:
    level, _hit = parse_reason(reason)
    if not level or not is_blocking_level(level):
        return "not_adult"
    if store is None or not cid:
        return "no_store"
    conv = {
        "conversation_id": cid,
        "platform": str((payload or {}).get("platform") or ""),
        "account_id": str((payload or {}).get("account_id") or "default"),
        "chat_key": str((payload or {}).get("chat_key") or ""),
    }
    ts = float(now if now is not None else time.time())
    cfg = None
    try:
        cfg = getattr(_bound_service(), "_cfg", None)
    except Exception:
        cfg = None
    persona = resolve_persona(conv, cfg)
    policy, _src = adult_policy_of(persona, cfg)
    if policy != "human":
        return f"policy_{policy}"
    prev = _kv_get(store, cid)
    try:
        if prev and ts - float(prev.get("ts") or 0) < FOLLOWUP_SEC:
            return "recently_sent"
    except (TypeError, ValueError):
        pass
    lang = str((payload or {}).get("lang") or "")
    ok = schedule_followup(store, conv, level=level, tag_ts=ts, lang=lang, cfg=cfg)
    return "scheduled" if ok else "not_scheduled"


# ── drafts.py 唯一钩子 ───────────────────────────────────────────────────────

def regrade_inbound(svc: Any, conv: Dict[str, Any], text: str, lang: str, risk_level: str,
                    peer_reasons: Sequence[str], risk_hits: Sequence[str], *,
                    automation_mode: str = "auto_ai", cfg: Any = None, persona: Any = None,
                    now: Optional[float] = None,
                    send: bool = True,
                    ctx: Any = None) -> Tuple[str, List[str], Optional[Dict[str, Any]]]:
    """``(risk_level, peer_reasons, info)``。无成人命中 → 原样返回、info=None。

    副作用（explicit / pressure 且政策非 mark_only）：``risk_hold.set(adult)`` + ``tag_needs_human``
    （reason ``adult:<level>:<hit>``）+ soft_reply 政策立即软回应。任何异常 → 原判定放行。

    ``ctx``（Q-23 #303 ``GuardContext``，None = 逐字旧行为）：群 / 非客户发送方 → **不评估、不出站、
    不打标、不持有**（``info.skipped=group|sender:<kind>``，原判定原样返回）；``manual`` / ``review`` /
    ``multi_choice`` 档 → 打标 / 持有照旧，但软回应**不发**，改为审核稿候选（reasons 追加
    ``adult_soft_alt:<text>``，与 Q-2 ``commitment_alt:`` 同式）。
    """
    reasons = [str(r) for r in (peer_reasons or [])]
    try:
        bind_service(svc)
        cid = str((conv or {}).get("conversation_id") or "")
        if ctx is not None:
            _skip = ""
            try:
                _skip = str(ctx.skip_reason() or "")
            except Exception:
                _skip = ""
            if _skip:
                logger.info("[guard-ctx] skip=%s stage=adult conv=%s %s", _skip, cid or "-", ctx.log_tag())
                return risk_level, reasons, {
                    "level": "", "hits": [], "pressure_hits": [], "policy": "", "policy_source": "",
                    "category": CATEGORY, "soft_reply": "", "needs_human": False, "skipped": _skip,
                }
        if cfg is None:
            cfg = getattr(svc, "_cfg", None)
        g = grade(text, lang, cfg=cfg)
        level = str(g.get("level") or "")
        _bare_adult = any(r == CATEGORY or str(r).startswith(REASON_PREFIX) for r in reasons)
        if not level and not _bare_adult:
            return risk_level, reasons, None
        store = getattr(svc, "_store", None)
        if not level:
            # quick_analyze 词表命中但本模块四级词表没认出（两表演进不同步时）：政策 open 时
            # 同样让路（否则裸 adult 主因 → risk_grader adult 中级）；其它政策维持旧行为不改判。
            if persona is None:
                persona = resolve_persona(conv, cfg)
            if not blocks_adult_outbound(conv, cfg) and adult_policy_of(persona, cfg)[0] == POLICY_OPEN:
                return _open_passthrough(cid, risk_level, reasons, level="", hits=[],
                                         src=adult_policy_of(persona, cfg)[1])
            return risk_level, reasons, None
        if blocks_adult_outbound(conv, cfg):
            hits = [str(h) for h in (g.get("hits") or [])]
            logger.info("[adult] grade conv=%s level=%s hits=%s action=skip_public_chat",
                        cid, level, "|".join(hits[:4]) or "-")
            return risk_level, reasons, {
                "level": level, "hits": hits,
                "pressure_hits": list(g.get("pressure_hits") or []),
                "policy": "", "policy_source": "", "category": CATEGORY,
                "soft_reply": "", "needs_human": False, "skipped": "public_chat",
            }
        if persona is None:
            persona = resolve_persona(conv, cfg)
        policy, src = adult_policy_of(persona, cfg)
        hits = [str(h) for h in (g.get("hits") or [])]
        hit0 = hits[0] if hits else ""
        info: Dict[str, Any] = {"level": level, "hits": hits, "pressure_hits": list(g.get("pressure_hits") or []),
                                "policy": policy, "policy_source": src, "category": CATEGORY,
                                "soft_reply": "", "needs_human": False}
        base = [r for r in reasons if r != CATEGORY and not r.startswith(REASON_PREFIX)
                and r not in (FLIRT_REASON, MARK_REASON)]
        _max = _max_risk
        if policy == POLICY_OPEN:
            # 成人不设限：分级只留日志，判定方向不变——不软回应 / 不打标 / 不持有 / 不 needs_human
            # （pressure 也不）。原 high 若只因成人而 high → 回 low；别的高危因子（索钱 / 未成年 /
            # 诈骗 / 自伤）不在本模块手里，risk_grader 照常判。
            return _open_passthrough(cid, risk_level, reasons, level=level, hits=hits, src=src,
                                     pressure_hits=list(g.get("pressure_hits") or []))
        if level in ("mention", "flirt"):
            new_risk = _max(_downgrade_from_adult(risk_level, reasons), "medium")
            out = [FLIRT_REASON] + base + [f"{REASON_PREFIX}{level}"]
            logger.info("[adult] grade conv=%s level=%s hits=%s policy=%s action=pass risk=%s",
                        cid, level, "|".join(hits[:4]) or "-", policy, new_risk)
            return new_risk, out, info
        if level == "explicit" and policy == "mark_only":
            new_risk = _max(_downgrade_from_adult(risk_level, reasons), "medium")
            out = [MARK_REASON] + base + [f"{REASON_PREFIX}{level}"]
            logger.info("[adult] grade conv=%s level=%s hits=%s policy=%s action=mark_only risk=%s",
                        cid, level, "|".join(hits[:4]) or "-", policy, new_risk)
            return new_risk, out, info
        _mode = str(getattr(ctx, "mode", "") or automation_mode or "").strip().lower()
        if level == "explicit":
            # Q-27（#301 追加 AFD2CD）：露骨**无施压** → 中级。不 risk_hold、不 needs_human（全自动不掐停）：
            #   · soft_reply 政策 × auto_ai → Q-23 单一闸门人设口吻短生成软回应（失败不发、无固定句）；
            #     调用方 drafts 见 soft_reply_status=scheduled 即不另拟稿（软回应就是本轮回复，避免两连发）；
            #   · soft_reply × manual/review/multi_choice → 审核稿候选 adult_soft_alt:<mode>（Q-23 口径不变）；
            #   · human 政策 → 「接住」：不转人工，正常拟稿由人设 prompt（adult_policy 块）带过。
            new_risk = _max(_downgrade_from_adult(risk_level, reasons), "medium")
            out = [SOFT_REASON] + base + [f"{REASON_PREFIX}{level}"]
            if hit0:
                out.append(f"adult_hit:{hit0[:40]}")
            ts = float(now if now is not None else time.time())
            action = "persona_catch"
            if policy == "soft_reply" and send and _mode and _mode != "auto_ai":
                out.append(f"{SOFT_ALT_PREFIX}{_mode}")
                info["soft_reply_status"] = "review_candidate"
                action = "soft_reply_candidate"
                logger.info("[adult] soft_reply conv=%s level=%s policy=%s mode=immediate status=review_candidate "
                            "automation_mode=%s", cid, level, policy, _mode)
            elif policy == "soft_reply" and send:
                res = send_soft_reply(store, conv, level=level, policy=policy, mode="immediate",
                                      persona=persona, lang=lang, cfg=cfg, svc=svc, now=ts, tag_ts=ts,
                                      peer_text=text, ctx=ctx, automation_mode=_mode or automation_mode)
                info["soft_reply"] = str(res.get("text") or "")
                info["soft_reply_status"] = str(res.get("status") or "")
                action = "soft_reply"
            logger.info("[adult] grade conv=%s level=%s hits=%s policy=%s(%s) action=%s risk=%s mode=%s "
                        "hold=none needs_human=false", cid, level, "|".join(hits[:4]) or "-", policy, src,
                        action, new_risk, automation_mode)
            return new_risk, out, info
        # pressure（露骨 + 施压）× 任何政策：high + needs_human + risk_hold（安全地板，Q-27 唯一保留的成人硬拦）
        new_risk = _max(risk_level, "high")
        out = [CATEGORY] + base + [f"{REASON_PREFIX}{level}"]
        if hit0:
            out.append(f"adult_hit:{hit0[:40]}")
        ts = float(now if now is not None else time.time())
        tag_reason = f"{REASON_PREFIX}{level}" + (f":{hit0[:40]}" if hit0 else "")
        info["needs_human"] = True
        if store is not None and cid:
            try:
                from src.inbox import risk_hold as _rh
                _rh.set(store, cid, CATEGORY, hits[:3] or level, by="adult_grader", now=ts)
            except Exception:
                logger.debug("[adult] risk_hold.set 失败（忽略）", exc_info=True)
            try:
                from src.integrations.protocol_autoreply import tag_needs_human
                # Q-27 C：打标行必带 level / category / hits（chip / 横幅 / 摘标冷却同源）
                tag_needs_human(store, {"platform": conv.get("platform"), "account_id": conv.get("account_id"),
                                        "chat_key": conv.get("chat_key"), "lang": lang},
                                reason=tag_reason, source="adult_grader", now=ts,
                                level="high", category=CATEGORY,
                                hits=[f"{CATEGORY}:{h[:40]}" for h in hits[:3]] or [f"{CATEGORY}:{level}"])
            except Exception:
                logger.debug("[adult] tag_needs_human 失败（忽略）", exc_info=True)
        action = "handoff"
        if policy == "soft_reply" and send and _mode and _mode != "auto_ai":
            # Q-23 #303：人在环档位（manual / review / multi_choice）任何自动出站只能是审核稿候选——
            # 软回应不发，候选写进 reasons（drafts 落进草稿 risk_reasons，坐席卡片可见）。
            out.append(f"{SOFT_ALT_PREFIX}{_mode}")
            info["soft_reply_status"] = "review_candidate"
            action = "soft_reply_candidate"
            logger.info("[adult] soft_reply conv=%s level=%s policy=%s mode=immediate status=review_candidate "
                        "automation_mode=%s", cid, level, policy, _mode)
        elif policy == "soft_reply" and send:
            res = send_soft_reply(store, conv, level=level, policy=policy, mode="immediate",
                                  persona=persona, lang=lang, cfg=cfg, svc=svc, now=ts, tag_ts=ts,
                                  peer_text=text, ctx=ctx, automation_mode=_mode or automation_mode)
            info["soft_reply"] = str(res.get("text") or "")
            info["soft_reply_status"] = str(res.get("status") or "")
            action = "soft_reply"
        elif policy == "human":
            action = "handoff_followup"   # 3 分钟补发由 tag_needs_human 钩子登记
        logger.info("[adult] grade conv=%s level=%s hits=%s pressure=%s policy=%s(%s) action=%s mode=%s",
                    cid, level, "|".join(hits[:4]) or "-", "|".join(info["pressure_hits"][:3]) or "-",
                    policy, src, action, automation_mode)
        return new_risk, out, info
    except Exception:
        logger.debug("[adult] regrade 异常（原判定放行）", exc_info=True)
        return risk_level, reasons, None


def _open_passthrough(cid: str, risk_level: str, reasons: Sequence[str], *, level: str,
                      hits: Sequence[str], src: str,
                      pressure_hits: Optional[Sequence[str]] = None) -> Tuple[str, List[str], Dict[str, Any]]:
    """``open`` 政策的统一出口：剥掉所有成人主因 / 标签（``adult`` / ``adult:*`` / adult_flirt /
    adult_mark / adult_soft / adult_hit:* / adult_soft_alt:*），原 high 若只因成人 → low；info 带
    ``action=none`` 供诊断卡 / 日志。"""
    _strip = (FLIRT_REASON, MARK_REASON, SOFT_REASON)
    out = [r for r in (reasons or [])
           if r != CATEGORY and not str(r).startswith(REASON_PREFIX) and r not in _strip
           and not str(r).startswith("adult_hit:") and not str(r).startswith(SOFT_ALT_PREFIX)]
    new_risk = _downgrade_from_adult(risk_level, reasons)
    hs = [str(h) for h in (hits or [])]
    logger.info("[adult] grade conv=%s level=%s hits=%s policy=%s(%s) action=none risk=%s->%s "
                "hold=none needs_human=false", cid or "-", level or "-", "|".join(hs[:4]) or "-",
                POLICY_OPEN, src, risk_level, new_risk)
    return new_risk, out, {
        "level": level, "hits": hs, "pressure_hits": [str(h) for h in (pressure_hits or [])],
        "policy": POLICY_OPEN, "policy_source": src, "category": CATEGORY,
        "soft_reply": "", "needs_human": False, "action": "none",
    }


def _downgrade_from_adult(risk_level: str, reasons: Sequence[str]) -> str:
    """原 high 若**只**因 adult 而 high → 回到 low（再由调用方抬到 medium）；有别的高危因子
    （支付词表 keyword / 隐私 / 停联…）则不动。"""
    others = [r for r in (reasons or []) if r != CATEGORY and not str(r).startswith(REASON_PREFIX)]
    if str(risk_level or "low").lower() == "high" and not others:
        return "low"
    return str(risk_level or "low")


def _max_risk(a: str, b: str) -> str:
    try:
        from src.inbox.autosend_policy import max_risk
        return max_risk(a, b)
    except Exception:
        rank = {"low": 0, "medium": 1, "high": 2}
        return a if rank.get(str(a).lower(), 0) >= rank.get(str(b).lower(), 0) else b


def card_label_parts(reason: Any) -> Dict[str, str]:
    """前端 / 诊断共用：``adult:explicit:sex`` → ``{category: adult, level: explicit, hit: sex}``。"""
    level, hit = parse_reason(reason)
    return {"category": CATEGORY if level else "", "level": level, "hit": hit}


__all__ = [
    "CATEGORY", "LEVELS", "POLICIES", "FOLLOWUP_SEC", "KV_PREFIX", "REASON_PREFIX",
    "FLIRT_REASON", "MARK_REASON", "SOFT_REASON",
    "grade", "neutralize_cum", "phrase_whitelist", "config_phrase_whitelist", "is_ambiguous_hit",
    "is_blocking_level", "normalize_policy",
    "blocks_adult_outbound",
    "default_policy", "adult_policy_of",
    "resolve_persona", "prompt_block", "soft_reply_candidates", "pick_soft_reply", "soft_reply_for",
    "last_soft_reply", "bind_service", "dispatch_soft_reply", "send_soft_reply",
    "run_followup", "schedule_followup", "pending_followups", "cancel_followup",
    "parse_reason", "on_needs_human_tagged", "regrade_inbound", "card_label_parts",
]
