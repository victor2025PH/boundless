"""出站「媒体承诺」守卫（纯函数，可单测/离线）。

背景（线上实录事故，见 ``image_autosend`` 模块 docstring）：文本回复与图片发送是
两条互不通气的链路——**是否发图**由客户入站文本的关键词判定
（``detect_selfie_request`` / ``plan_autosend_image``），而 LLM 的**出站文本**却可能
自行承诺「等我拍一张给你」「马上发你」。两边从不核对 → 客户等不到图质问
「你快拍啊，是不是骗我的」，直接击穿"真人感"信任。

职责（只做判定与文本处理，不碰 IO；接线在 autosend_helpers / skill_manager）：

- ``detect_media_promise(text)``：出站文本是否含「即刻要发照片/语音」的承诺。
  刻意窄口径——只抓**第一人称 + 即时**的断言；远期承诺（改天/下次）、否认句
  （发不了照片）、疑问 offer（要不要我拍）、过去指涉（上次那张）都不算
  （宁可漏报不误伤——漏报最多少撤回一句，误报会把正常话剥掉）。
- ``strip_media_promises(text)``：句级剥离承诺句（无 LLM 可用时的正则兜底）。
- ``build_promise_rewrite_instruction(text, kind)``：LLM 重写指令（首选撤回路径，
  任意语言可靠；正则只认 zh/en）。
- ``detect_media_offer(text)`` + ``is_short_affirmative(text)`` + ``offer_accepted``：
  「offer-接受」桥——上一轮 AI 问「要不要看照片」、本轮客户只回「好呀」时，
  ``detect_selfie_request("好呀")`` 抓不住 → offer 变空头支票；桥把这种短肯定
  视同一次要图请求。
- ``deflection_line(sample, kind)``：撤回后整句被剥空时的语言对齐兜底话术。

调用方处理顺序约定：**兑现优先，撤回兜底**——文本承诺了照片先尝试真发
（``run_autosend_image(assume_intent="selfie")``，预算/关系闸门照常生效）；
发不出才重写/剥离。诚实只有两种形态：要么兑现、要么闭嘴。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

KIND_IMAGE = "image"
KIND_VOICE = "voice"

# ── 句子切分（剥离粒度=整句：承诺句常带"你等着哈"这类跟班短语，整句剥最干净）──
_SENT_SPLIT_RE = re.compile(r"([。！？!?～~;；…\n]+)")


def _sentences(text: str) -> List[str]:
    """按句末标点切句（保内容不保定界符，用于逐句判定）。"""
    parts = _SENT_SPLIT_RE.split(str(text or ""))
    return [p for i, p in enumerate(parts) if i % 2 == 0 and p.strip()]


# ── 承诺模式（zh + en；只认第一人称即时承诺/"照片来了"式无图断言）────────────
_IMG_PROMISE = [re.compile(p, re.IGNORECASE) for p in (
    # zh：等我拍/我这就去拍/我马上拍
    r"等\s*(?:我|人家)\s*(?:去)?\s*拍",
    r"(?:我|人家)\s*(?:这就|這就|马上|馬上|现在|現在|立刻|待会儿?|待會兒?|等下|一会儿?|一會兒?)\s*(?:去)?\s*拍",
    r"(?:我|人家)\s*去\s*拍",
    # zh：拍(一张)(照片)发/给/传你、拍张给你看
    r"拍\s*(?:一?[张張]|好|完)?\s*(?:照片|自拍|相片)?\s*(?:就)?\s*(?:发|發|传|傳|给|給)\s*(?:给|給)?\s*你",
    r"拍\s*(?:一?[张張]|个|個)?\s*给\s*你\s*看",
    # zh：发你(一张)照片/给你看(我的)照片
    r"(?:发|發|传|傳|给|給)\s*你\s*(?:一?[张張])?\s*(?:照片|自拍|相片|美照|靓照|图|圖)",
    r"给\s*你\s*看\s*(?:一?[张張])?\s*(?:我的)?\s*(?:照片|自拍|相片)",
    # zh：照片马上发你/自拍这就传过去
    r"(?:照片|自拍|相片|美照)\s*(?:马上|馬上|这就|這就|待会儿?|等下|一会儿?)?\s*(?:就)?\s*(?:发|發|传|傳)\s*(?:给|給)?\s*(?:你|过去|過去)",
    # zh：无图硬说"到了"（照片来啦/拍好啦发你）
    r"(?:照片|自拍)\s*(?:来|來)\s*(?:啦|了|咯|喽)",
    r"拍\s*好\s*(?:啦|了)\s*(?:发|發|给|給)",
    # zh：刚拍/已发断言（2026-07-14 真机漏报："来啦刚拍的～"/"这不已经发给你了嘛"）
    r"刚\s*(?:才)?\s*拍\s*(?:的|好|完|好啦|好了)",
    r"(?:这)?(?:已经|就)\s*(?:发|發|传|傳)\s*(?:给|給)\s*你",
    r"(?:照片|图|圖|自拍)\s*(?:已经|就)\s*(?:发|發|给|給)",
    # zh：翻相册式承诺（2026-07-14 真机漏报："这就给你翻张新的"——无"拍/发+照片"
    # 结构，旧正则全漏 → 客户等图等到质问）。"给你翻/找/挑(一)张"基本只在承诺给图。
    r"(?:给|給)\s*你\s*(?:翻|找|挑)\s*(?:一?[张張])",
    # en
    r"\bi(?:'|’)?ll\s+(?:go\s+)?(?:send|take|snap|shoot|grab)\s+(?:you\s+)?(?:a|an|one|another|some)?\s*(?:photos?|pic(?:ture)?s?|selfies?)",
    r"\blet\s+me\s+(?:go\s+)?(?:take|snap|send|grab|shoot)\s+(?:you\s+)?(?:a|an|one|another)?\s*(?:photo|pic(?:ture)?|selfie)",
    r"\b(?:i(?:'|’)?m\s+)?(?:gonna|going\s+to)\s+(?:send|take|snap)\s+(?:you\s+)?(?:a|an|one)?\s*(?:photo|pic(?:ture)?|selfie)",
    r"\bsending\s+(?:you\s+)?(?:a|an|one|another)?\s*(?:photo|pic(?:ture)?|selfie)",
    r"\b(?:photo|pic(?:ture)?|selfie)\s+(?:is\s+)?(?:on\s+(?:the|its)\s+way|coming|incoming)",
    r"\bhere(?:'|’)?s\s+(?:a|an|my|one)?\s*(?:photo|pic(?:ture)?|selfie)",
    # ja（Phase18 多语扩展；只收宣告形——"写真(を)送るね/送ります"、"今から撮る"）
    r"(?:写真|自撮り|セルフィー)\s*(?:を)?\s*(?:送|おく)(?:る|り|っ)",
    r"(?:今|いま)から\s*撮(?:る|り|っ)",
    r"撮\s*って\s*(?:送|おく)",
    # ko（"사진/셀카 보낼게/보내줄게"、"찍어서 보낼"）
    r"(?:사진|셀카)\s*(?:을|를)?\s*보내?\s*(?:줄게|줄께|드릴게|드릴께|께|ㄹ게)",
    r"(?:사진|셀카)[^\n]{0,6}보낼",
    r"찍어서\s*보낼",
    # es（"te mando/envío una foto"、"te tomo una selfie"）
    r"\bte\s+(?:mando|env[ií]o|tomo)\s+(?:una?|otra)\s*(?:foto|selfie)",
    r"\bahora\s+te\s+mando\s+(?:la|una)\s+foto",
    # fr（"je t'envoie une photo"、"je vais te prendre une photo"）
    r"\bje\s+t[' ]envoie\s+une\s+photo",
    r"\bje\s+vais\s+te\s+(?:prendre|envoyer)\s+une\s+photo",
    # pt（"te mando uma foto"、"vou te mandar uma foto/selfie"）
    r"\bte\s+mando\s+uma\s+(?:foto|selfie)",
    r"\bvou\s+te\s+mandar\s+uma\s+(?:foto|selfie)",
)]

_VOICE_PROMISE = [re.compile(p, re.IGNORECASE) for p in (
    r"(?:发|發|传|傳)\s*你\s*(?:一?[条條段个個])?\s*(?:语音|語音)",
    r"给\s*你\s*(?:发|發|录|錄)\s*(?:一?[条條段个個])?\s*(?:语音|語音)",
    r"(?:我|人家)\s*(?:去)?\s*(?:录|錄)\s*(?:一?[条條段个個])?\s*(?:语音|語音)",
    r"等\s*我\s*(?:发|發|录|錄)\s*(?:语音|語音)",
    r"(?:语音|語音)\s*(?:说|說|讲|講)\s*给\s*你",
    r"\bi(?:'|’)?ll\s+(?:send|record)\s+(?:you\s+)?(?:a|an|one)?\s*voice",
    r"\blet\s+me\s+(?:send|record)\s+(?:you\s+)?(?:a|an)?\s*voice",
    r"\bsending\s+(?:you\s+)?a\s+voice",
    # ja / ko / es（宣告形语音承诺；宁漏勿误）
    r"(?:ボイス|音声|ボイスメッセージ)\s*(?:を)?\s*(?:送|おく)(?:る|り|っ)",
    r"(?:음성|보이스)[^\n]{0,6}보낼",
    r"\bte\s+mando\s+un\s+(?:audio|mensaje\s+de\s+voz)",
)]

# ── 排除面（命中任一 → 该句不算承诺）──────────────────────────────────────────
_EXCLUDES = [re.compile(p, re.IGNORECASE) for p in (
    # 否认/拒绝——本身就是"发不了"的诚实表达，剥掉反而变谎
    r"不能|不方便|没法|沒法|无法|無法|发不了|發不了|不发|不發|不给|不給|别发|別發|不会发|不會發|不许|不許",
    r"\bcan(?:'|’)?t\b|\bcannot\b|\bunable\b|\bwon(?:'|’)?t\b|\bno\s+photos?\b",
    r"送れない|送れません|撮れない|撮れません",
    r"못\s*보내|안\s*보내",
    r"\bno\s+puedo\b|\bje\s+ne\s+peux\s+pas\b|\bn[ãa]o\s+posso\b",
    # 远期/条件承诺——不可即时证伪，常是合理社交话术（改天拍给你）
    r"改天|下次|以后|以後|回头|回頭|有机会|有機會|哪天|下回|过几天|過幾天|周末|週末|明天|到时候|到時候|见面|見面|等你来|等妳来",
    r"\bsomeday\b|\bnext\s+time\b|\bsome\s+other\s+time\b|\bone\s+day\b|\bwhen\s+we\s+meet\b|\btomorrow\b|\blater\s+this\b",
    r"今度|こんど|明日|あした|いつか",
    r"다음에|나중에|내일",
    r"\bma[ñn]ana\b|\bdemain\b|\bamanh[ãa]\b|\bla\s+pr[óo]xima\b",
    # 疑问/offer（要不要我拍）——不是断言；由 offer-accept 桥接管
    r"要不要|要嗎|要吗|想不想|好不好|可以吗|可以嗎|行不行|\bwant\s+me\s+to\b|\bshould\s+i\b|\bdo\s+you\s+want\b",
    r"送ろうか|送りましょうか|보내줄까|\bquieres\s+que\b|\bveux-tu\s+que\b|\bquer\s+que\b",
    # 过去指涉——谈论已发生的照片
    r"上次|之前|那[张張]|昨天|前几天|前幾天|以前|刚才发|剛才發|\blast\s+time\b|\bearlier\b|\bthat\s+(?:photo|pic)\b",
    r"この前|さっき送|昨日|아까\s*보낸|지난번",
)]

_QUESTION_TAIL_RE = re.compile(r"[?？]\s*$")


def _sentence_is_promise(sent: str) -> str:
    """单句判定：返回 'image'/'voice'/''。疑问句/排除面命中一律不算。"""
    s = str(sent or "").strip()
    if not s:
        return ""
    if _QUESTION_TAIL_RE.search(s):
        return ""
    for ex in _EXCLUDES:
        if ex.search(s):
            return ""
    for rx in _IMG_PROMISE:
        if rx.search(s):
            return KIND_IMAGE
    for rx in _VOICE_PROMISE:
        if rx.search(s):
            return KIND_VOICE
    return ""


def detect_media_promise(text: str) -> str:
    """出站文本是否承诺「即刻发照片/语音」。返回 'image'/'voice'/''（image 优先）。

    注意：整条文本按句判定——疑问句结尾的句子不算（offer 语义），但同条里
    其他陈述句照常判。
    """
    found = ""
    for sent in _sentences(text):
        k = _sentence_is_promise(sent)
        if k == KIND_IMAGE:
            return KIND_IMAGE
        if k and not found:
            found = k
    return found


def strip_media_promises(text: str) -> str:
    """句级剥离承诺句（保定界符结构；剥空返回空串，由调用方兜底）。

    只在「承诺无法兑现」时调用；正常文本原样返回（零副作用）。
    """
    raw = str(text or "")
    if not raw.strip():
        return raw
    parts = _SENT_SPLIT_RE.split(raw)
    out: List[str] = []
    i = 0
    while i < len(parts):
        seg = parts[i]
        delim = parts[i + 1] if i + 1 < len(parts) else ""
        if seg.strip() and _sentence_is_promise(seg):
            i += 2
            continue  # 丢句 + 尾随定界符
        out.append(seg)
        if delim:
            out.append(delim)
        i += 2
    res = "".join(out).strip()
    # 剥后只剩标点/空白 → 视同剥空
    if res and not re.search(r"[\w\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", res):
        return ""
    return res


def build_promise_rewrite_instruction(text: str, kind: str = KIND_IMAGE) -> str:
    """LLM 撤回重写指令（首选路径；任意语言可靠）。只输出改写后的消息正文。"""
    what = "语音" if kind == KIND_VOICE else "照片"
    t = str(text or "").strip()[:400]
    return (
        f"下面这条聊天消息答应了要发{what}，但{what}这一轮实际发不出去。\n"
        f"请改写这条消息：删掉所有「要发/正在发/等我拍/来了」这类关于{what}的承诺或暗示，"
        "其余内容、语言、语气、长度尽量保持原样；"
        "如果整条消息都在说这件事，就用同样的语言和语气写一句自然地岔开话题的话"
        "（比如先聊聊天、卖个关子），不要道歉、不要解释系统原因。\n"
        "只输出改写后的消息正文，不要引号。\n"
        f"原消息：「{t}」"
    )


# ── 撤回兜底话术（剥空时用；按文本文字系统取 zh/ja/ko/en）─────────────────────
_KANA_RE = re.compile(r"[\u3040-\u30ff]")      # 平假名/片假名 → ja
_HANGUL_RE = re.compile(r"[\uac00-\ud7af]")    # 谚文 → ko
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")       # 汉字（无假名时）→ zh

_DEFLECTIONS = {
    KIND_IMAGE: {
        "zh": "嘿嘿，先卖个关子～多陪我聊聊嘛😊",
        "ja": "ふふ、それはまた今度のお楽しみ〜もっとお話しよ？😊",
        "ko": "히히 그건 다음 기회에~ 나랑 얘기 더 하자😊",
        "en": "hehe, let me keep you curious~ chat with me a bit more first 😊",
    },
    KIND_VOICE: {
        "zh": "先打字聊嘛，回头哄哄我再说～",
        "ja": "まずはメッセージでお話しよ〜？",
        "ko": "일단 문자로 얘기하자~ 나중에 잘해주면 몰라도😉",
        "en": "let's just text for now, sweet-talk me a bit first~",
    },
}


def _script_lang(sample: str) -> str:
    """按文字系统粗分语言（假名→ja、谚文→ko、汉字→zh、其余→en）。
    日文常混汉字，须先测假名再测汉字。拉丁语种（es/fr/pt）统一走 en 兜底。"""
    s = str(sample or "")
    if _KANA_RE.search(s):
        return "ja"
    if _HANGUL_RE.search(s):
        return "ko"
    if _CJK_RE.search(s):
        return "zh"
    return "en"


def deflection_line(sample_text: str, kind: str = KIND_IMAGE) -> str:
    """整句被剥空后的语言对齐兜底：轻巧岔开话题、不否认能力、不做新承诺。"""
    table = _DEFLECTIONS.get(kind if kind in _DEFLECTIONS else KIND_IMAGE, {})
    return table.get(_script_lang(sample_text), table.get("en", ""))


# ── offer-accept 桥（上一轮 AI 提议发照片、本轮客户短肯定 → 视同要图请求）──────
_OFFER_IMG = [re.compile(p, re.IGNORECASE) for p in (
    r"要不要.{0,10}(?:照片|自拍|相片|拍[一]?[张張]|看看我)",
    r"想不想看.{0,8}(?:我|照片|自拍)",
    r"想看.{0,8}(?:照片|自拍|我的?样子|我的?樣子)",
    r"(?:拍|发|發|传|傳)\s*(?:一?[张張])?\s*(?:照片|自拍)?\s*给\s*你\s*看?\s*(?:吗|嗎|好不好|要不要|要吗|要嗎)",
    r"\bwant\s+(?:me\s+)?to\s+(?:send|take)\s+(?:you\s+)?a\s+(?:photo|pic(?:ture)?|selfie)",
    r"\bwanna\s+see\s+(?:a\s+|my\s+)?(?:photo|pic(?:ture)?|selfie|face)",
)]

_AFFIRM_CORE = (
    r"(?:好+[呀啊哦的呢]?|要的?|要看|想看?|想看看|嗯+|恩+|行+|可以[呀啊]?|"
    r"发吧|發吧|拍吧|来吧|來吧|快发|快發|快拍|发来|發來|来|來|看看|给我看看|給我看看|"
    r"ok(?:ay)?|yes+|yeah+|yep|sure|please|pls|go\s+ahead|send(?:\s+it)?|show\s+me|wanna\s+see)"
)
# 允许叠词/连用（「好呀好呀」「嗯嗯 发吧」——Phase8 事故用户原话就是叠词短肯定）
_AFFIRM_RE = re.compile(
    r"^(?:" + _AFFIRM_CORE + r"[，,、!！~～。.\s]*){1,3}[😊😍🥰❤️]*$",
    re.IGNORECASE,
)


def detect_media_offer(text: str) -> str:
    """出站 AI 文本是否在**提议**发照片（要不要看…）。返回 'image'/''。"""
    s = str(text or "")
    if not s.strip():
        return ""
    for rx in _OFFER_IMG:
        if rx.search(s):
            return KIND_IMAGE
    return ""


def is_short_affirmative(text: str) -> bool:
    """客户短肯定（好呀/要/嗯嗯/ok/sure…，≤16 字符）。长句不算（可能带新话题）。"""
    s = str(text or "").strip()
    if not s or len(s) > 16:
        return False
    return bool(_AFFIRM_RE.match(s))


def offer_accepted(peer_text: str, history: Optional[Sequence[Dict[str, Any]]]) -> str:
    """「offer-接受」桥：客户本条是短肯定，且最近一条 assistant 消息在提议发照片
    → 返回 'image'（视同要图请求）；否则 ''。history 取 [{role, content}] 序列。"""
    if not is_short_affirmative(peer_text):
        return ""
    for m in reversed(list(history or [])):
        if str((m or {}).get("role") or "") == "assistant":
            return detect_media_offer(str((m or {}).get("content") or ""))
    return ""


__all__ = [
    "KIND_IMAGE", "KIND_VOICE",
    "detect_media_promise", "strip_media_promises",
    "build_promise_rewrite_instruction", "deflection_line",
    "detect_media_offer", "is_short_affirmative", "offer_accepted",
]
