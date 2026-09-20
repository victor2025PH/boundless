# -*- coding: utf-8 -*-
"""地区语气档（#40，2026-09-04）——「像不像那个地方的人在说话」的单一事实源。

报障原话（钧）：「设定的 AI 是台湾人……句尾常带『呀』『啦』『耶』『嘛』不常见，
做不到聊天起来像是台湾女生的语气」「所有的聊天看起来都像是大陆的语气」。

现状：spoken_style_bridge 的 L1/L2 是单一套中文口语指令，没有地区维度。本模块
给口语层加**地区档**（zh-CN / zh-TW / zh-HK 起步，注册表结构留扩展位），由
spoken_style_bridge.system_block 叠加在现有说话指纹之上。

与翻译栈的边界（#36 前半已交付、勿重复劳动）：本模块**不管用什么字**（简/繁由
出向翻译层按「发→」决定），只管语气词 / 句式 / 禁用词。唯一例外是 HK 档——
粤语口语书写本身就是繁体字形，指令里点名（与 dialect_flavor=cantonese 同口径）。

来源优先级（缺一级往下走；全部软失败）：
  1. 人设显式地区字段：``speaking.region`` / 顶层 ``region``（坐席明确设了就听它）
  1b. 人设 ``voice_profile.dialect_flavor == cantonese`` → zh-HK（粤语人设天然 HK 档）
  1c. 人设居住地（persona_location 已解析的 country：TW/HK/MO/CN）
  2. 会话「发→」语言变体（B67 显式铆定）：zh-tw → zh-TW，yue → zh-HK
  3. 全局默认 ``ai.spoken_style.region``，缺省 zh-CN

**回归钉**：zh-CN 档 ``region_block`` 恒返回空串——存量部署（无人设地区/无「发→」
变体）的 prompt 逐字不变。禁用词那一半比语气词更重要（一句「咋整」就当场破功）。
"""
from __future__ import annotations

import logging
import re
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

REGION_CN = "zh-CN"
REGION_TW = "zh-TW"
REGION_HK = "zh-HK"
DEFAULT_REGION = REGION_CN

# 别名 → 规范档位。键一律小写、去空白/下划线/连字符后比对。
_ALIASES: Dict[str, str] = {
    # zh-CN
    "zhcn": REGION_CN, "cn": REGION_CN, "zh": REGION_CN, "zhhans": REGION_CN,
    "china": REGION_CN, "mainland": REGION_CN, "prc": REGION_CN,
    "大陆": REGION_CN, "大陸": REGION_CN, "中国大陆": REGION_CN, "内地": REGION_CN,
    "簡體": REGION_CN, "简体": REGION_CN,
    # zh-TW
    "zhtw": REGION_TW, "tw": REGION_TW, "twn": REGION_TW, "taiwan": REGION_TW,
    "zhhant": REGION_TW, "台湾": REGION_TW, "臺灣": REGION_TW, "台灣": REGION_TW,
    "台北": REGION_TW, "臺北": REGION_TW, "taipei": REGION_TW,
    # zh-HK（澳门并入 HK 档：同为粤语口语区）
    "zhhk": REGION_HK, "hk": REGION_HK, "hkg": REGION_HK, "hongkong": REGION_HK,
    "香港": REGION_HK, "yue": REGION_HK, "cantonese": REGION_HK, "粤语": REGION_HK,
    "粵語": REGION_HK, "广东话": REGION_HK, "廣東話": REGION_HK,
    "zhmo": REGION_HK, "mo": REGION_HK, "macau": REGION_HK, "macao": REGION_HK,
    "澳门": REGION_HK, "澳門": REGION_HK,
}

# 居住地 country（persona_location.PersonaPlace.country，ISO-2）→ 档位
_COUNTRY_TO_REGION: Dict[str, str] = {
    "TW": REGION_TW,
    "HK": REGION_HK,
    "MO": REGION_HK,
    "CN": REGION_CN,
}

# 「发→」变体（outbound_translate.normalize_target 输出）→ 档位。
# 刻意不含 zh：「发→简体中文」不改变人设自己的地区档（只是字形要求）。
_OUTBOUND_TO_REGION: Dict[str, str] = {
    "zh-tw": REGION_TW,
    "yue": REGION_HK,
}

_NORM_RE = re.compile(r"[\s_\-–—/·.]+")


def normalize_region(value: Any) -> str:
    """任意写法 → 规范档位（zh-CN / zh-TW / zh-HK）；认不出返回空串（调用方往下级走）。"""
    s = str(value or "").strip()
    if not s:
        return ""
    key = _NORM_RE.sub("", s).lower()
    if not key:
        return ""
    return _ALIASES.get(key, "")


# ─────────────────────────── 档位内容（三样：语气词 / 句式 / 禁用词） ────────────

# 每档一份 profile。CN 档 block 恒空（回归钉），只保留元数据便于看板/测试枚举。
REGION_PROFILES: Dict[str, Dict[str, Any]] = {
    REGION_CN: {
        "label": "中国大陆",
        "particles": [],          # 存量行为=不额外指令
        "patterns": [],
        "avoid": [],
        "script": "",
    },
    REGION_TW: {
        "label": "台灣",
        # 句尾/语气词：报障点名的「啦/耶/嘛」+ 台湾口语高频「喔/欸/齁/呦/捏/唷」
        "particles": ["啦", "耶", "喔", "欸", "嘛", "齁", "呦", "捏", "唷", "餒"],
        # 句式偏好：软化问句 + 台湾日常口语词
        "patterns": [
            "問句偏軟：「有沒有…」「會不會…」「要不要…」「是不是…」，少用直接的「幹嘛」「幹啥」",
            "口語詞用台灣習慣：「蠻…的」「超…的」「還不錯欸」「真的假的」「好喔」「對啊」「醬子」",
            "稱呼與量詞偏台灣：「你們」不說「你們這幫」；「機車」指摩托車；「腳踏車」不說「自行車」",
        ],
        # 禁用词：大陆网络/北方口语特征词——一句话蹦出一个就当场破功
        "avoid": [
            "咋", "咋整", "咋办", "咋样", "咋樣", "整挺好", "牛逼", "牛批", "老铁", "老鐵",
            "贼", "忒", "得劲", "带劲", "麻溜", "杠杠的", "哥们", "哥們", "小老弟",
            "啥", "干啥", "幹啥", "干哈", "幹哈", "咱", "咱们", "咱們", "俺",
            "666", "yyds", "绝绝子", "絕絕子", "给力", "給力", "整个活", "整活",
            "搞对象", "搞對象", "扯犊子", "扯犢子",
            "土豆", "西红柿", "西紅柿", "自行车", "自行車", "视频", "視頻",
        ],
        "script": "",  # 字形跟翻译层/对方（不在本档职责内）
    },
    REGION_HK: {
        "label": "香港",
        # 粤语口语虚词
        "particles": ["啦", "喎", "囉", "咩", "呀", "嘅", "㖭", "啫", "咋", "喇", "唧", "嘞"],
        "patterns": [
            "用粵語口語書寫：「係」不說「是」、「唔」不說「不」、「嘅」不說「的」、「咩」不說「什麼」、「點解」不說「為什麼」、「幾時」不說「什麼時候」",
            "問句常帶「係唔係」「得唔得」「好唔好」「有冇」；肯定句尾多「啦」「囉」「喎」",
            "港式用詞：「返工」「放工」「食飯」「飲茶」「巴士」「的士」「搭車」「屋企」「好正」「勁」",
        ],
        "avoid": [
            "咋整", "咋办", "牛逼", "老铁", "老鐵", "贼", "忒", "得劲", "哥们", "哥們",
            "咱", "咱们", "咱們", "俺", "666", "yyds", "绝绝子", "絕絕子", "给力", "給力",
            "干啥", "幹啥", "咋样", "咋樣", "扯犊子", "扯犢子",
            # 普通话书面虚词——粤语口语里出现＝出戏
            "什么", "什麼", "为什么", "為什麼", "怎么", "怎麼", "没有", "沒有", "不是", "的话", "的話",
        ],
        "script": "繁體",
    },
}

_REGION_ORDER = (REGION_CN, REGION_TW, REGION_HK)


def known_regions() -> tuple:
    """已注册档位（看板/选择器枚举用）。"""
    return _REGION_ORDER


def region_profile(region: str) -> Dict[str, Any]:
    """取档位 profile（未知档位 → CN 档，即空指令）。"""
    return REGION_PROFILES.get(normalize_region(region) or region, REGION_PROFILES[REGION_CN])


def region_block(region: str) -> str:
    """L1 system 追加段：该地区「像本地人说话」的指令块。zh-CN / 未知 → 空串（回归钉）。"""
    reg = normalize_region(region)
    if not reg or reg == REGION_CN:
        return ""
    p = REGION_PROFILES.get(reg)
    if not p:
        return ""
    lines = [f"【地區語氣｜{p['label']}】你是{p['label']}人，中文要像{p['label']}本地人在聊天，而不是大陸腔。"]
    if p.get("script"):
        # 字形硬钉子（I-4 续，2026-09-04）：#36 实弹实锤只说「繁體」LLM 会出「有冇/食饭」
        # 简体粤文混排——把最高频的简体字当负样本点名，比一句「全程繁體」硬得多。
        lines.append(
            f"- 【字形硬性要求】每一個字都用{p['script']}字，粵語用字也一樣（係/唔/嘅/咩/佢/喺/嗰）。"
            "簡體字一個都不許出現，尤其這些最容易漏的：" + "、".join(_SIMPLIFIED_MARKERS) + "。"
        )
    if p.get("particles"):
        lines.append("- 句尾語氣詞自然地用這些（別每句都加，兩三句出現一次就好）：" + "、".join(p["particles"]) + "。")
    for pat in p.get("patterns") or []:
        lines.append("- " + pat)
    if p.get("avoid"):
        lines.append("- 【最重要】絕對不要用這些大陸/北方口語與網路詞，一個都不行：" + "、".join(p["avoid"]) + "。")
    return "\n".join(lines)


# ─────────────────────────── 禁用词命中观测（不改文本，只计数） ────────────────

# 高频「简体独有」字（对应繁体字形不同）：只用于 script=繁體 档的出站观测——
# 命中＝LLM 漏了字形要求。刻意只挑简繁**必不同**的字（「的/是」简繁同形不在内），
# 且都是日常聊天里几乎每句必出的字，漏一个就抓得到。
_SIMPLIFIED_MARKERS: tuple = (
    "这", "说", "吗", "们", "么", "为", "会", "发", "时", "间", "过", "还", "没",
    "来", "对", "后", "个", "开", "见", "让", "请", "谢", "点", "东", "买", "钱",
    "饭", "习", "书", "车", "电", "话", "问", "题", "关", "系", "现", "样",
)
_SIMPLIFIED_RE = re.compile("[" + "".join(_SIMPLIFIED_MARKERS) + "]")


def find_simplified_chars(text: str, region: str) -> list:
    """繁體档回复里出现的简体特征字（去重、按出现顺序）。非繁體档 / 空文本恒空。

    与 ``find_banned_words`` 同哲学：只观测不改写（简→繁改写归 #36 的出向翻译栈，
    这里再做一套＝双源）。
    """
    reg = normalize_region(region)
    if not reg or not text:
        return []
    if not (REGION_PROFILES.get(reg, {}).get("script")):
        return []
    seen: list = []
    for ch in _SIMPLIFIED_RE.findall(text):
        if ch not in seen:
            seen.append(ch)
    return seen

# 单字禁用词只在「独立成词」高置信位置计数（否则「賊船」「忒斯拉」全误报）；
# 没登记上下文正则的单字（咱/俺/啥/咋）本身就是特征字，直接子串匹配。
_SINGLE_CHAR_CTX_RE = {
    "贼": re.compile(r"贼(?=[好多快慢香帅美强牛贵便])"),
    "忒": re.compile(r"忒(?=[好多快慢香贵便])"),
}


def find_banned_words(text: str, region: str) -> list:
    """返回回复里命中的该档禁用词（去重、按出现顺序）。CN 档恒空。

    单字词走上下文正则（保守，宁漏不误）；多字词直接子串匹配。
    只用于观测/测试断言，**不改写文本**（改写会引入新的失败面）。
    """
    reg = normalize_region(region)
    if not reg or reg == REGION_CN or not text:
        return []
    avoid = REGION_PROFILES.get(reg, {}).get("avoid") or []
    hits: list = []
    low = text.lower()
    for w in avoid:
        if not w:
            continue
        rx = _SINGLE_CHAR_CTX_RE.get(w)
        if rx is not None:
            if rx.search(text) and w not in hits:
                hits.append(w)
            continue
        if w.lower() in low and w not in hits:
            hits.append(w)
    return hits


# ─────────────────────────── 解析：人设 → 会话 → 全局 ─────────────────────────

_STATS_LOCK = threading.Lock()
_STATS: Dict[str, int] = {
    "resolve_persona": 0,      # 由人设显式字段决定
    "resolve_dialect": 0,      # 由粤语 dialect_flavor 决定
    "resolve_location": 0,     # 由人设居住地推断
    "resolve_outbound": 0,     # 由会话「发→」变体决定
    "resolve_default": 0,      # 回落全局默认
    "banned_hit": 0,           # 出站回复命中禁用词（观测）
    "script_hit": 0,           # 繁體档回复漏出简体特征字（观测）
    "observed": 0,             # 非 CN 档出站回复被观测的总条数（命中率分母）
}


def _bump(key: str) -> None:
    with _STATS_LOCK:
        _STATS[key] = _STATS.get(key, 0) + 1


def stats() -> Dict[str, int]:
    with _STATS_LOCK:
        return dict(_STATS)


def _persona_explicit_region(persona: Any) -> str:
    if not isinstance(persona, dict):
        return ""
    sp = persona.get("speaking")
    if isinstance(sp, dict):
        r = normalize_region(sp.get("region"))
        if r:
            return r
    return normalize_region(persona.get("region"))


def _persona_dialect_region(persona: Any) -> str:
    if not isinstance(persona, dict):
        return ""
    vp = persona.get("voice_profile")
    if not isinstance(vp, dict):
        return ""
    flavor = str(vp.get("dialect_flavor") or "").strip().lower()
    if not flavor:
        return ""
    try:
        from src.ai.cosy_dialect import normalize_dialect_flavor
        flavor = str(normalize_dialect_flavor(flavor) or "").strip().lower()
    except Exception:
        pass
    return REGION_HK if flavor == "cantonese" else ""


def _persona_location_region(persona: Any) -> str:
    if not isinstance(persona, dict):
        return ""
    try:
        from src.companion.persona_location import resolve_place_with_fallback
        place = resolve_place_with_fallback(persona)
    except Exception:
        return ""
    if place is None:
        return ""
    return _COUNTRY_TO_REGION.get(str(getattr(place, "country", "") or "").upper(), "")


def outbound_variant_region(outbound_lang: Any) -> str:
    """「发→」变体 → 档位（zh-tw→TW / yue→HK；其余含 zh 不定档）。"""
    s = str(outbound_lang or "").strip().lower()
    return _OUTBOUND_TO_REGION.get(s, "")


def _conversation_outbound_region(context: Any) -> str:
    if not isinstance(context, dict):
        return ""
    # 上游若已解析出「发→」铆定，直接消费（避免重复查库）
    for k in ("_outbound_lang_pin", "outbound_lang", "_outbound_lang"):
        r = outbound_variant_region(context.get(k))
        if r:
            return r
    plat = str(context.get("platform") or context.get("channel") or "").strip()
    acct = str(context.get("account_id") or "").strip()
    chat = str(context.get("chat_key") or context.get("chat_id") or "").strip()
    if not (plat and acct and chat):
        return ""
    try:
        from src.ai.sendpoint_guard import outbound_lang_pin
        return outbound_variant_region(outbound_lang_pin(plat, acct, chat))
    except Exception:
        return ""


def resolve_region(
    persona: Any = None,
    *,
    context: Any = None,
    default: str = "",
    use_location: bool = True,
) -> str:
    """按优先级解析地区档；永远返回规范档位（最差 zh-CN），绝不抛。"""
    try:
        r = _persona_explicit_region(persona)
        if r:
            _bump("resolve_persona")
            return r
        r = _persona_dialect_region(persona)
        if r:
            _bump("resolve_dialect")
            return r
        if use_location:
            r = _persona_location_region(persona)
            if r:
                _bump("resolve_location")
                return r
        r = _conversation_outbound_region(context)
        if r:
            _bump("resolve_outbound")
            return r
    except Exception:
        logger.debug("persona_region.resolve_region 异常，回落默认", exc_info=True)
    _bump("resolve_default")
    return normalize_region(default) or DEFAULT_REGION


def resolve_region_from_context(config: Any, context: Any) -> str:
    """ai_client 调用口：从 ai.spoken_style 配置 + 本轮 context 解析档位。

    读 ``ai.spoken_style.region``（全局默认）与 ``ai.spoken_style.region_from_location``
    （默认开：人设居住地 TW/HK 视为该档）。context 缺席 → 全局默认。
    """
    cfg: Dict[str, Any] = {}
    try:
        root = (config.config or {}) if config is not None else {}
        c = ((root.get("ai") or {}).get("spoken_style") or {})
        cfg = c if isinstance(c, dict) else {}
    except Exception:
        cfg = {}
    persona = None
    if isinstance(context, dict):
        persona = context.get("_resolved_persona") or context.get("persona")
        # 会话级覆写（前端选择器落地后可写这里；I-2 落地前恒缺）
        r = normalize_region(context.get("_spoken_region"))
        if r:
            return r
    return resolve_region(
        persona,
        context=context,
        default=str(cfg.get("region") or ""),
        use_location=bool(cfg.get("region_from_location", True)),
    )


def note_banned_hits(reply: str, region: str) -> list:
    """出站观测：命中禁用词 / 繁體档漏简体字 就计数 + INFO 一行（不改文本）。

    返回禁用词命中列表（保持旧契约）；简体字命中只计 ``script_hit`` 与日志。
    ``observed`` 每调用 +1＝命中率分母（ops 卡按 hit/observed 读）。
    """
    _bump("observed")
    hits = find_banned_words(reply, region)
    if hits:
        _bump("banned_hit")
        logger.info("[persona_region] %s 档回复命中禁用词 %s", region, hits)
    sc = find_simplified_chars(reply, region)
    if sc:
        _bump("script_hit")
        logger.info("[persona_region] %s 档回复漏出简体字 %s", region, sc)
    # 持久灰度账本（logs/i4_gray/region_*.jsonl）：L4 负样本改写要不要开，看的是跨
    # 重启的命中率；只落地区/命中词/长度，不落回复原文。
    try:
        from src.ops import region_quote_gray
        region_quote_gray.append("region", {
            "region": region, "banned": list(hits), "script": list(sc),
            "len": len(reply or ""),
        })
    except Exception:  # noqa: BLE001
        pass
    return hits
