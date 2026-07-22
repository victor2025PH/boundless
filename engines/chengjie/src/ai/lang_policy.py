"""会话语言策略（单一事实源）——把「逐条猜语言」升级为「会话级语言契约」。

治理三类线上事故（2026-07 用户反馈）：
  1. 用户明确说「用日语聊」→ 请求内容被无视，回复语言仍镜像消息书写语言；
  2. 中文会话里发一个品牌词「whatsapp」→ 被当英语强证据，整条回复翻成英文，
     且误判写进会话缓存持续污染后续轮次；
  3. 方言/口音语音被 ASR 误判语种 → 转写文本"确认"错误语言并锁定回复语言。

核心设计（与旧逻辑的关键差异）：
  - **语言是会话状态，不是逐条检测结果**。决策优先级：
      运营锁定 > 用户明确请求（持久） > 强证据检测（立即跟随） >
      粘住上一轮 / 窗口主导语言 > 默认。
  - **证据分级**：剥离「语言中性 token」（品牌名/ok/URL/数字/emoji）后再检测；
    脚本级命中（假名/谚文/CJK/阿拉伯…）= 强证据，当条即切（保留跟随敏捷性）；
    含糊拉丁短文本 = 弱证据，永不触发切换（只够在无历史时兜底）。
  - **明确语言请求是意图不是检测**：多语言正则解析「说日语 / 日本語で話して /
    speak japanese / 한국어로 말해줘」→ 输出目标语言码，一次生效、持久跟踪。

纯函数、零新依赖（复用 translation_service.detect_language 确定性核心）、可单测。
本模块不落库——持久化由调用方决定（skill_manager 用 ContextStore，收件箱产线用
``latest_explicit_request(history)`` 从历史纯函数恢复，天然免迁移）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "EvidenceStrength",
    "PolicyDecision",
    "strip_neutral_tokens",
    "parse_language_request",
    "classify_evidence",
    "latest_explicit_request",
    "resolve_conversation_language",
    "normalize_lang_code",
    "contains_language_alias",
    "valid_lang_code",
]


# ══════════════════════════════════════════════════════════════════════
# 一、语言中性 token（不构成任何语言证据的内容）
# ══════════════════════════════════════════════════════════════════════

# 品牌/平台/业务词：全球用户跨语言通用，出现在任何会话里都不代表「切换到英语」。
_NEUTRAL_WORDS = frozenset({
    # 通讯与社交平台
    "whatsapp", "wa", "telegram", "tg", "line", "wechat", "weixin", "qq",
    "messenger", "facebook", "fb", "instagram", "ins", "ig", "tiktok",
    "douyin", "twitter", "x", "youtube", "yt", "signal", "viber", "skype",
    "zalo", "kakao", "kakaotalk", "discord", "snapchat", "linkedin",
    # 商业/加密/支付高频词
    "usdt", "usd", "btc", "eth", "trc20", "erc20", "vpn", "app", "apk",
    "ios", "android", "iphone", "google", "apple", "gmail", "email", "id",
    "vip", "ok", "okay", "okey", "kk", "pdf", "excel", "word", "ppt",
    # 聊天填充词（多语用户通用，不构成英语证据）
    "yes", "no", "yeah", "yep", "nope", "hi", "hello", "hey", "bye",
    "thx", "ty", "thanks", "thank", "pls", "plz", "please", "sorry",
    "lol", "omg", "brb", "asap", "gm", "gn", "haha", "hahaha", "hmm",
    "em", "emm", "en", "oh", "ah", "wow", "oops", "ya", "ha",
})

_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_MENTION_RE = re.compile(r"@[A-Za-z0-9_.\-]+")
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
_NUM_RE = re.compile(r"[+\-]?\d[\d,.\-:/]*")
# emoji 与符号块（含变体选择符/国旗/杂项符号）
_EMOJI_RE = re.compile(
    r"[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0000FE00-\U0000FE0F"
    r"\U0001F1E0-\U0001F1FF\u2190-\u21FF\u2B00-\u2BFF]+"
)
_PUNCT_ONLY_RE = re.compile(r"^[\s!-/:-@\[-`{-~。，、！？；：「」『』（）…—·～\u3000]*$")


def strip_neutral_tokens(text: str) -> str:
    """剥离语言中性内容，返回可作为语言证据的「实质文本」。

    剥离：URL / @mention / email / 数字串 / emoji / 品牌与填充词（词边界，大小写无关）。
    返回残余文本（已压缩空白）。残余为空 = 本条消息不构成任何语言证据。
    """
    t = str(text or "")
    if not t.strip():
        return ""
    t = _URL_RE.sub(" ", t)
    t = _MENTION_RE.sub(" ", t)
    t = _EMAIL_RE.sub(" ", t)
    t = _EMOJI_RE.sub(" ", t)
    t = _NUM_RE.sub(" ", t)

    # 中性词按「拉丁词边界」剥离——只影响拉丁 token，不碰 CJK 文本
    def _drop_neutral(m: "re.Match[str]") -> str:
        return "" if m.group(0).lower() in _NEUTRAL_WORDS else m.group(0)

    t = re.sub(r"[A-Za-z][A-Za-z'&.]*", _drop_neutral, t)
    t = re.sub(r"\s+", " ", t).strip()
    if _PUNCT_ONLY_RE.match(t):
        return ""
    return t


# ══════════════════════════════════════════════════════════════════════
# 二、明确语言请求解析（意图，不是检测）
# ══════════════════════════════════════════════════════════════════════

# 语言名称 → 语言码（覆盖中/日/英/韩文书写的语言名；粤语暂不进 alias，
# 下游 TTS/LANGUAGE RULE 尚无 yue 全链路支持，P2 专项引入）。
_LANG_ALIASES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("zh", ("中文", "汉语", "漢語", "华语", "華語", "普通话", "普通話", "国语", "國語",
            "chinese", "mandarin", "中国語", "중국어")),
    ("ja", ("日语", "日語", "日文", "japanese", "日本語", "일본어")),
    ("en", ("英语", "英語", "英文", "english", "英語で", "영어", "englisch", "inglés", "inglês")),
    ("ko", ("韩语", "韓語", "韩文", "韓文", "korean", "韓国語", "한국어", "한국말")),
    ("es", ("西班牙语", "西班牙語", "西语", "西語", "spanish", "español", "espanol", "castellano")),
    ("pt", ("葡萄牙语", "葡萄牙語", "葡语", "葡語", "portuguese", "português", "portugues")),
    ("fr", ("法语", "法語", "法文", "french", "français", "francais", "フランス語")),
    ("de", ("德语", "德語", "德文", "german", "deutsch", "ドイツ語")),
    ("ru", ("俄语", "俄語", "俄文", "russian", "русский", "по-русски", "ロシア語")),
    ("th", ("泰语", "泰語", "泰文", "thai", "ภาษาไทย", "ไทย", "タイ語")),
    ("vi", ("越南语", "越南語", "越语", "越語", "vietnamese", "tiếng việt", "tieng viet")),
    ("id", ("印尼语", "印尼語", "indonesian", "bahasa indonesia", "bahasa")),
    ("ar", ("阿拉伯语", "阿拉伯語", "arabic", "العربية", "اردو", "urdu", "乌尔都语", "烏爾都語")),
    ("it", ("意大利语", "意大利語", "italian", "italiano")),
    ("tr", ("土耳其语", "土耳其語", "turkish", "türkçe", "turkce")),
    ("hi", ("印地语", "印地語", "hindi", "हिंदी", "हिन्दी")),
)

_ALIAS_TO_CODE: Dict[str, str] = {}
for _code, _names in _LANG_ALIASES:
    for _n in _names:
        _ALIAS_TO_CODE[_n.lower()] = _code

# 单字语言名（中文口语常用「说日语」「用英文」——单字 + 语/文 后缀）
_CJK_SINGLE: Dict[str, str] = {
    "中": "zh", "汉": "zh", "漢": "zh", "华": "zh", "華": "zh",
    "日": "ja", "英": "en", "韩": "ko", "韓": "ko", "法": "fr",
    "德": "de", "俄": "ru", "西": "es", "葡": "pt", "泰": "th",
    "越": "vi", "阿": "ar", "意": "it",
}

# alias 联合正则（长名优先，防止「日本語」只匹到「日」）
_ALIAS_UNION = "|".join(
    sorted((re.escape(a) for a in _ALIAS_TO_CODE), key=len, reverse=True)
)
_CJK_LANG_TOKEN = r"(?:[中汉漢华華日英韩韓法德俄西葡泰越阿意][语語文]|" + _ALIAS_UNION + r")"

# —— 中文句式 ——
# 请求动词 + 语言名（+ 可选交流动词/礼貌尾缀）。示例：说日语 / 用日文聊 / 换成英文 /
# 请用日语回复 / 改用韩语 / 跟我说日语吧 / 咱们用英语交流
_ZH_REQ_RE = re.compile(
    r"(?:请|請|麻烦|麻煩)?\s*(?:跟我|和我|与我|與我|咱们|咱們|我们|我們)?\s*"
    r"(?:说|說|讲|講|用|使用|换成|換成|换回|換回|换|換|改用|改成|切换到|切換到|切换成|切換成|切回|切到)"
    r"\s*(" + _CJK_LANG_TOKEN + r")"
    r"\s*(?:聊|说|說|讲|講|交流|沟通|溝通|回复|回覆|回答|回我|写|寫)?",
)
# 「X语/X文 + 回复/交流」前置形式：日文回复我 / 英文交流
_ZH_REQ_PREFIX_RE = re.compile(
    r"(" + _CJK_LANG_TOKEN + r")\s*(?:回复|回覆|回答|回我|交流|沟通|溝通|聊)",
)
# 「还是 X 吧」口语回切形式：算了还是中文吧 / 还是英文好了
_ZH_REQ_BACK_RE = re.compile(
    r"(?:还是|還是)\s*(" + _CJK_LANG_TOKEN + r")\s*(?:吧|好了|行|可以|来|來)?",
)
# 转述句（排除）：客户说要用日语聊 / 他又说要用英文 —— 引述他人，不是对话请求
_ZH_REPORTED_RE = re.compile(
    r"(?:他|她|客户|客戶|有人|朋友|那人|对方|對方|同事|老板|老闆)\s*(?:又|也|还|還|就)?\s*[说說]"
    r"|[说說]\s*(?:要|想要?)\s*(?:用|说|說|讲|講)"
)
# 能力陈述/疑问（排除）：会说日语(吗) / 我会日语 / 你懂英文么 —— 陈述或试探，不是指令。
# 注意「能不能/能否/可以…吗」是礼貌请求，不在此列（见 _ZH_POLITE_REQ_RE）。
_ZH_ABILITY_RE = re.compile(
    r"(?:会|會|懂)\s*(?:说|說|讲|講)?\s*" + _CJK_LANG_TOKEN
)
# 礼貌请求：能不能说日语 / 你能否用英文 / 可以说日语吗（可以/能 需带疑问尾）
_ZH_POLITE_REQ_RE = re.compile(
    r"(?:你|妳)?(?:能不能|能否)\s*(?:说|說|讲|講|用)\s*(" + _CJK_LANG_TOKEN + r")"
    r"|(?:你|妳)?(?:可以|能)\s*(?:说|說|讲|講|用)\s*(" + _CJK_LANG_TOKEN + r")"
    r"\s*(?:聊|回复|回覆|交流)?\s*[吗嗎]?\s*[?？]"
)
# 否定（排除）：别说日语 / 不要用英文 / 别再发日语 / 我唔系讲英文（粤语「不是」）/ 我不是讲英文
# 含粤语否定「唔系/唔係/唔喺」与「不是/不係」——治「我唔系讲英文…讲中文」被 讲英文 误匹成 en。
_ZH_NEG_RE = re.compile(
    r"(?:别|別|不要|不用|勿|少|不是|不係|唔系|唔係|唔喺)\s*(?:再)?\s*"
    r"(?:说|說|讲|講|用|发|發)\s*" + _CJK_LANG_TOKEN
)

# —— 英文句式 ——
_EN_LANG_UNION = _ALIAS_UNION
_EN_REQ_RE = re.compile(
    r"(?:can|could|would|will)?\s*(?:you|u)?\s*(?:pls|plz|please)?\s*"
    r"(?:speak|talk|chat|reply|answer|respond|write|text|type|message|communicate)"
    r"(?:\s+(?:to|with)\s+me)?\s*(?:in|with|using)?\s+(" + _EN_LANG_UNION + r")\b",
    re.IGNORECASE,
)
_EN_SWITCH_RE = re.compile(
    r"(?:switch|change)\s+(?:to|into)\s+(" + _EN_LANG_UNION + r")\b"
    r"|\bin\s+(" + _EN_LANG_UNION + r")\s*,?\s*(?:pls|plz|please)\b"
    r"|\buse\s+(" + _EN_LANG_UNION + r")\b",
    re.IGNORECASE,
)
_EN_NEG_RE = re.compile(
    r"(?:don'?t|do\s+not|stop|no\s+more|quit)\s+"
    r"(?:speak|speaking|talk|talking|reply|replying|use|using|write|writing)\b",
    re.IGNORECASE,
)
# 「看不懂 X」负向请求：I can't understand/read Chinese → 切离 X（用消息自身语言）
_CANT_UNDERSTAND_RE = re.compile(
    r"(?:can'?t|cannot|don'?t|do\s+not)\s+(?:understand|read|speak)\s+(" + _EN_LANG_UNION + r")\b",
    re.IGNORECASE,
)
_ZH_CANT_UNDERSTAND_RE = re.compile(
    r"(?:看不懂|听不懂|聽不懂|不懂|读不懂|讀不懂)\s*(" + _CJK_LANG_TOKEN + r")"
)

# —— 日文句式 —— 日本語で話して / 英語でお願いします / 中国語にして
_JA_REQ_RE = re.compile(
    r"(" + _ALIAS_UNION + r"|[日英中韓]?\S{0,2}語)\s*"
    r"(?:で\s*(?:話|はな|喋|しゃべ|お願|おねが|たの|頼|書|か|返事|へんじ|やり取り|やりとり|チャット)"
    r"|に\s*(?:して|切り替え|きりかえ|変えて|かえて))",
)
_JA_LANG_WORD: Dict[str, str] = {
    "日本語": "ja", "英語": "en", "中国語": "zh", "中國語": "zh",
    "韓国語": "ko", "韓國語": "ko", "フランス語": "fr", "ドイツ語": "de",
    "ロシア語": "ru", "タイ語": "th", "スペイン語": "es",
}

# —— 韩文句式 —— 한국어로 말해줘 / 영어로 대답해 / 일본어로 해줘
_KO_REQ_RE = re.compile(
    r"(한국어|한국말|영어|일본어|중국어|불어|독일어|러시아어|태국어|스페인어)\s*로\s*"
    r"(?:말|얘기|이야기|대화|대답|답|답장|해|써|보내)"
)
_KO_LANG_WORD: Dict[str, str] = {
    "한국어": "ko", "한국말": "ko", "영어": "en", "일본어": "ja",
    "중국어": "zh", "불어": "fr", "독일어": "de", "러시아어": "ru",
    "태국어": "th", "스페인어": "es",
}


def _alias_code(name: str) -> str:
    n = str(name or "").strip().lower()
    if not n:
        return ""
    if n in _ALIAS_TO_CODE:
        return _ALIAS_TO_CODE[n]
    # 中文单字 + 语/文 后缀（说日语 → 「日语」）
    if len(n) >= 2 and n[-1] in ("语", "語", "文") and n[0] in _CJK_SINGLE:
        return _CJK_SINGLE[n[0]]
    return _JA_LANG_WORD.get(name, "") or _KO_LANG_WORD.get(name, "")


# 语言名提及探测（LLM 短判兜底的廉价门控）：消息里出现任何语言名 token 才值得
# 花一次 LLM 短判去理解隐晦请求（"my chinese is bad, sorry…"）。
_ALIAS_MENTION_RE = re.compile(
    r"(?:" + _ALIAS_UNION + r"|[中汉漢华華日英韩韓法德俄西葡泰越阿意][语語文])",
    re.IGNORECASE,
)


def contains_language_alias(text: str) -> bool:
    """消息是否提及任何语言名（gating 用，不判断语义）。"""
    t = str(text or "")
    return bool(t and _ALIAS_MENTION_RE.search(t))


def valid_lang_code(code: str) -> str:
    """归一并校验语言码是否为策略层已知语言；未知返回 ""（防 LLM 幻觉码）。"""
    c = normalize_lang_code(code)
    known = {c2 for c2, _ in _LANG_ALIASES}
    return c if c in known else ""


def parse_language_request(text: str) -> str:
    """解析「用户明确要求的回复语言」→ 语言码；无明确请求返回 ""。

    只认「指令/请求」语义，能力疑问（会说日语吗）与否定（别说英文）都不算。
    支持中文/英文/日文/韩文四种书写的请求句式 + 「看不懂 X」负向请求。
    误伤代价高（突然切错语言），故取保守口径：宁漏勿错，漏网句式由
    P1 的 LLM 短判兜底扩展。
    """
    t = str(text or "").strip()
    if not t or len(t) > 120:  # 长文本里的语言名多为转述，不当指令
        return ""

    # 英文否定 / 转述他人 → 硬排除（返回 ""）。中文否定不再硬排除，改为「屏蔽被否定的
    # 语言片段后继续找正向请求」——因为一条消息可同时含否定与正向请求
    # （「我唔系讲英文，讲中文啊」：否定 en + 正向 zh，正确结果是 zh 而非 "" 或 en）。
    if _EN_NEG_RE.search(t) or _ZH_REPORTED_RE.search(t):
        return ""

    # 「看不懂/听不懂 X」→ 切到消息自身的书写语言（前提：≠ X 且可判定）
    m = _CANT_UNDERSTAND_RE.search(t) or _ZH_CANT_UNDERSTAND_RE.search(t)
    if m:
        away = _alias_code(m.group(1))
        own_lang, strength = classify_evidence(t)
        if own_lang and own_lang != away and strength != EvidenceStrength.NONE:
            return own_lang
        return ""

    # 礼貌请求（先于能力排除）：能不能说日语 / 可以用英文聊吗？
    m = _ZH_POLITE_REQ_RE.search(t)
    if m:
        name = next((g for g in m.groups() if g), "")
        code = _alias_code(name)
        if code:
            return code

    # 能力陈述/疑问：会说日语吗 / 我会说日语 / 你懂英文么 —— 无条件排除。
    # 中文语境下这是试探或自述，不是指令；真想切换的用户会接着说「那用日语吧」。
    # （英文 can you speak X? 保留请求语义——多为非英语母语者求救信号，由 _EN_REQ_RE 命中。）
    if _ZH_ABILITY_RE.search(t):
        return ""

    # 屏蔽「被否定的语言片段」（别说日语 / 我唔系讲英文），只在剩余文本里找正向请求：
    # 纯否定（「别说日语了」）屏蔽后无正向残留 → 自然返回 ""；
    # 否定+正向（「唔系讲英文…讲中文」）屏蔽掉 讲英文 后 讲中文 命中 → zh。
    zt = _ZH_NEG_RE.sub("　", t)
    for pat in (_ZH_REQ_RE, _ZH_REQ_PREFIX_RE, _ZH_REQ_BACK_RE):
        m = pat.search(zt)
        if m:
            code = _alias_code(m.group(1))
            if code:
                return code

    m = _EN_REQ_RE.search(t)
    if m:
        code = _alias_code(m.group(1))
        if code:
            return code
    m = _EN_SWITCH_RE.search(t)
    if m:
        name = next((g for g in m.groups() if g), "")
        code = _alias_code(name)
        if code:
            return code

    m = _JA_REQ_RE.search(t)
    if m:
        code = _alias_code(m.group(1))
        if code:
            return code

    m = _KO_REQ_RE.search(t)
    if m:
        code = _KO_LANG_WORD.get(m.group(1), "")
        if code:
            return code

    return ""


# ══════════════════════════════════════════════════════════════════════
# 三、证据强度分级
# ══════════════════════════════════════════════════════════════════════

class EvidenceStrength:
    """语言证据强度（字符串常量，无枚举依赖，便于日志与序列化）。"""
    NONE = "none"        # 剥离中性内容后无实质文本 → 不构成任何证据
    WEAK = "weak"        # 含糊拉丁短文本 → 只够无历史时兜底，永不触发切换
    STRONG = "strong"    # 脚本级/关键词级确定命中 → 可立即跟随、可写缓存


_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_HANGUL_RE = re.compile(r"[\uac00-\ud7af]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_NONLATIN_SCRIPT_RE = re.compile(
    r"[\u0e01-\u0e4e\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff\u0400-\u04ff"
    r"\u0900-\u097f\u0590-\u05ff\u0370-\u03ff\u1780-\u17ff]"
)
# 全局 detect_language 未覆盖、但既有产线（ai_client）支持的脚本 → 本层先拦截
_EXTRA_SCRIPTS: Tuple[Tuple[str, "re.Pattern"], ...] = (
    ("pa", re.compile(r"[\u0a00-\u0a7f]")),   # 古木基文（旁遮普语）
    ("bn", re.compile(r"[\u0980-\u09ff]")),   # 孟加拉文
)


def normalize_lang_code(code: str) -> str:
    """归一各产线语言码到策略层标准码（zh-cn/cn→zh、jp→ja、ar_ur→ar…）。

    WhatsApp RPA 的 TTS 码（zh-cn）、ai_client 的 ar_ur 等历史码在进入策略层
    之前都先归一，避免「同语言不同码」被误判为语言切换。
    """
    c = str(code or "").strip().lower().replace("_", "-")
    if not c:
        return ""
    aliases = {
        "zh-cn": "zh", "zh-tw": "zh", "cn": "zh",
        "jp": "ja", "kr": "ko", "ar-ur": "ar", "ur": "ar",
    }
    return aliases.get(c, c)


def classify_evidence(text: str) -> Tuple[str, str]:
    """返回 (语言码, 证据强度)。检测前先剥离语言中性内容。

    强证据（当条即可切换）：
      - 假名/谚文/泰文/阿拉伯/西里尔等独有脚本 ≥2 字符
      - CJK（中文）≥2 字符
      - 越南语变音符 / 拉丁语种关键词（hola/merci/danke…）命中
      - 拉丁实质文本 ≥12 字符或 ≥3 个词（成句英文）
    弱证据（只作兜底，绝不触发切换）：更短的含糊拉丁残余。
    """
    core = strip_neutral_tokens(text)
    if not core:
        return "", EvidenceStrength.NONE

    # 全局检测器未覆盖的脚本（旁遮普/孟加拉）——先拦截，保持与
    # ai_client._detect_message_language 的既有语种能力对齐。
    for _xl, _xp in _EXTRA_SCRIPTS:
        n = len(_xp.findall(core))
        if n:
            return (_xl, EvidenceStrength.STRONG if n >= 2 else EvidenceStrength.WEAK)

    from src.ai.translation_service import detect_language

    lang = detect_language(core)
    if not lang or lang == "unknown":
        return "", EvidenceStrength.NONE

    # 独有脚本：≥2 个脚本字符 = 强（1 个字符可能是引用/emoji 混杂）
    if _KANA_RE.search(core):
        n = len(_KANA_RE.findall(core))
        return ("ja", EvidenceStrength.STRONG) if n >= 2 else ("ja", EvidenceStrength.WEAK)
    if _HANGUL_RE.search(core):
        n = len(_HANGUL_RE.findall(core))
        return ("ko", EvidenceStrength.STRONG) if n >= 2 else ("ko", EvidenceStrength.WEAK)
    if lang not in ("en", "zh") and _NONLATIN_SCRIPT_RE.search(core):
        n = len(_NONLATIN_SCRIPT_RE.findall(core))
        return (lang, EvidenceStrength.STRONG) if n >= 2 else (lang, EvidenceStrength.WEAK)

    if lang == "zh":
        n = len(_CJK_RE.findall(core))
        return ("zh", EvidenceStrength.STRONG) if n >= 2 else ("zh", EvidenceStrength.WEAK)

    # 拉丁语种：detect_language 非 en 结果 = 关键词/变音命中 → 强
    if lang != "en":
        return lang, EvidenceStrength.STRONG

    # 含糊拉丁（en fallback）：成句才算强
    letters = len(_LATIN_RE.findall(core))
    words = len(re.findall(r"[A-Za-z][A-Za-z']*", core))
    if letters >= 12 or words >= 3:
        return "en", EvidenceStrength.STRONG
    if letters >= 4:
        return "en", EvidenceStrength.WEAK
    return "", EvidenceStrength.NONE


# ══════════════════════════════════════════════════════════════════════
# 四、会话级语言决策
# ══════════════════════════════════════════════════════════════════════

@dataclass
class PolicyDecision:
    """语言决策结果。

    lang:    本轮回复应使用的语言码
    source:  决策来源（operator_lock / explicit_request / user_pref /
             stable_switch / detected / sticky / window / weak_detect / default）
    request: 本条消息命中的明确语言请求（"" = 未命中）——调用方据此持久化偏好、
             打标签、发事件
    stable:  本条证据是否「稳定」——只有 stable=True 时才应更新会话语言缓存
    """
    lang: str
    source: str
    request: str = ""
    stable: bool = False


def _user_texts_newest_first(history: Optional[List[Dict[str, Any]]]) -> List[str]:
    out: List[str] = []
    for m in reversed(history or []):
        if not isinstance(m, dict):
            continue
        if m.get("role") == "user" or m.get("direction") in ("in", "inbound"):
            c = str(m.get("content") or m.get("text") or "").strip()
            if c:
                out.append(c)
    return out


def latest_explicit_request(
    history: Optional[List[Dict[str, Any]]],
    *,
    max_scan: int = 30,
) -> str:
    """从历史（新→旧）恢复最近一次明确语言请求；已被稳定漂移覆盖则返回 ""。

    释放规则（关键设计，见 resolve_conversation_language 文档）：请求之后若存在
    连续 ≥2 条强证据、且语言 L **既 ≠ 请求语言、又 ≠ 用户提出请求时的书写语言**，
    才视为真实语境变化、请求失效。「用户一直写他本来就在写的语言」不构成释放
    ——事故1 的原始场景正是「用中文请求日语后继续打中文」，此时偏好必须坚持。
    让无状态产线（收件箱草稿）无需任何存储即可获得「偏好持久」语义——历史即存储。
    """
    texts = _user_texts_newest_first(history)[:max_scan]
    # 只看「距今最近的一段连续强证据 run」——它代表用户当下真实在用的语言。
    run_lang = ""
    run_count = 0
    run_closed = False
    for t in texts:  # 新 → 旧
        req = parse_language_request(t)
        if req:
            req_input, _ = classify_evidence(t)  # 请求消息自身的书写语言
            if (
                run_count >= 2
                and run_lang
                and run_lang != req
                and run_lang != req_input
            ):
                return ""  # 请求之后已稳定漂移到「新」语言 → 请求失效
            return req
        lang, strength = classify_evidence(t)
        if strength == EvidenceStrength.STRONG and not run_closed:
            if not run_lang or lang == run_lang:
                run_lang, run_count = lang, run_count + 1
            else:
                run_closed = True  # 最近 run 被更早的异语言打断 → 定格
        # 弱/无证据不打断也不累计
    return ""


def _window_dominant(history: Optional[List[Dict[str, Any]]], k: int = 6) -> str:
    """近 k 条用户消息的主导语言（剥离中性内容后检测；无实质内容返回 ""）。"""
    texts = _user_texts_newest_first(history)[:k]
    if not texts:
        return ""
    joined = strip_neutral_tokens(" ".join(reversed(texts)))
    if len(joined) < 2:
        return ""
    from src.ai.translation_service import detect_language

    lang = detect_language(joined)
    return "" if (not lang or lang == "unknown") else lang


def resolve_conversation_language(
    text: str,
    history: Optional[List[Dict[str, Any]]] = None,
    *,
    prev_lang: str = "",
    lang_pref: str = "",
    lang_pref_input: str = "",
    operator_lock: str = "",
    default: str = "zh",
) -> PolicyDecision:
    """会话级语言决策（单一事实源）。

    优先级：
      1. operator_lock —— 运营手动锁定，绝对优先（request 仍解析并透出，供打标签）。
      2. 本条消息的明确语言请求 —— 立即生效并应持久（source=explicit_request）。
      3. lang_pref —— 既往明确请求的持久偏好（source=user_pref）。
      4. 本条强证据 —— 立即跟随（客户真换语言要跟上，source=detected）。
      5. 弱/无证据 —— 粘住 prev_lang（source=sticky）；再回落窗口主导语言（window）、
         弱检测（weak_detect）、default。

    偏好释放（关键护栏——请求时书写语言豁免）：
      事故1 的原始场景是「用中文请求日语，然后继续打中文」。若按「连续两条中文
      就释放」，偏好当场失效、bug 复现。故释放要求用户稳定漂移到**第三种语言** L：
      L ≠ 偏好语言 且 L ≠ lang_pref_input（用户提出请求时的书写语言），连续 ≥2 条
      强证据（本条 + 最近一条历史强证据消息）。lang_pref_input 未知（""）时取最保守
      口径：漂移永不释放，只有新的明确请求能改语言。

    stable 字段语义：调用方**只在 stable=True 时**把 lang 写进会话缓存
    （detected_lang 之类），弱证据/粘住结果不落盘——治「单条误判污染缓存」。
    """
    text = str(text or "")
    req = parse_language_request(text)

    lock = normalize_lang_code(operator_lock)
    if lock and lock not in ("auto", "detect"):
        return PolicyDecision(lang=lock, source="operator_lock", request=req, stable=True)

    if req:
        return PolicyDecision(lang=req, source="explicit_request", request=req, stable=True)

    pref = normalize_lang_code(lang_pref)
    pref_input = normalize_lang_code(lang_pref_input)
    lang, strength = classify_evidence(text)

    if pref:
        if (
            strength == EvidenceStrength.STRONG
            and lang
            and lang != pref
            and pref_input
            and lang != pref_input
        ):
            # 漂移释放：本条强证据 + 最近一条历史强证据同语言 → 连续段成立
            prev_texts = _user_texts_newest_first(history)
            # history 末条可能就是本条（调用方多把当前消息含在 history 里）→ 跳过
            cur = text.strip()
            scan = [t for t in prev_texts if t != cur][:3]
            for t in scan:
                if parse_language_request(t):
                    break
                l2, s2 = classify_evidence(t)
                if s2 == EvidenceStrength.STRONG:
                    if l2 == lang:
                        return PolicyDecision(
                            lang=lang, source="stable_switch", request="", stable=True
                        )
                    break  # 最近一条强证据是别的语言 → 连续段不成立
            # 连续段未成立 → 偏好仍然生效
        return PolicyDecision(lang=pref, source="user_pref", request="", stable=True)

    if strength == EvidenceStrength.STRONG and lang:
        return PolicyDecision(lang=lang, source="detected", request="", stable=True)

    prev = normalize_lang_code(prev_lang)
    if prev and prev not in ("auto", "unknown"):
        return PolicyDecision(lang=prev, source="sticky", request="", stable=False)

    win = _window_dominant(history)
    if win:
        return PolicyDecision(lang=win, source="window", request="", stable=False)

    if strength == EvidenceStrength.WEAK and lang:
        return PolicyDecision(lang=lang, source="weak_detect", request="", stable=False)

    return PolicyDecision(lang=default, source="default", request="", stable=False)
