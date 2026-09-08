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
# 视频通话承诺（2026-07-22 A2）：真机实录 AI 声称「我微信视频号同 WhatsApp 视频
# 都开到㗎」——系统并无视频通话能力，客户真拨即穿帮。与 image/voice 不同：
# 没有"兑现"路径，检测到只能撤回改写。
KIND_VIDEOCALL = "video_call"

# ── 句子切分（剥离粒度=整句：承诺句常带"你等着哈"这类跟班短语，整句剥最干净）──
# 英文句点只在后跟空白/行尾时算句界（#171 2026-09-05：实录是英文多句消息，
# 此前整段英文算一句，剥离＝整条清空；小数「3.5」/域名「a.com」无空白不切）。
_SENT_SPLIT_RE = re.compile(r"([。！？!?～~;；…\n]+|\.(?=\s|$))")


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
    # zh：宾语前置「(我这就)给你拍(一张X)」（实施69 实录漏网：「我这就给你拍一张
    # 刚烤好的大腰子」——「给你」插在副词与「拍」之间，上面各条全漏；该洞还连锁
    # 关掉下一轮 claim 门控的 last_reply 粘性判定，一个语序两道防线同时失守）。
    # 负向前瞻排除惯用语（拍手/拍马屁/拍肩/拍板…）与过去指涉（「给你拍过」）；
    # 「回头/改天给你拍」由 _EXCLUDES 远期组照常豁免；「你给你朋友拍」因
    # 「你」「拍」间有插入词不命中。
    r"(?:给|給)\s*你\s*拍(?!手|马|馬|肩|背|板|桌|脑|腦|过|過)\s*(?:一?[张張个個])?",
    r"(?:给|給)\s*你\s*来\s*(?:一?[张張])",
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
    # 粤语（2026-07-29 对练补漏：俾=给 睇=看 影=拍 而家=现在 即刻=马上）——
    # 「而家即刻拍俾你睇」「影张相畀你」这类将发承诺此前全漏。
    r"(?:而家|即刻|等我|待我)\s*(?:即刻|马上|馬上)?\s*(?:影|拍|影返|拍返)\s*(?:张|張|多|翻|返)?\s*(?:相|相片|自拍)?\s*(?:畀|俾|給|给)\s*你",
    r"(?:影|拍)\s*(?:多)?\s*(?:张|張|翻|返)?\s*(?:相|相片|自拍)?\s*(?:畀|俾)\s*你\s*(?:睇|睇下|睇吓)?",
)]

# 翻找式软承诺（2026-07-31 人设试聊实录三连：「等我找找看有没有存图」「我翻翻
# 手机相册哈」——无「拍/发」动词、无「给你」结构，_IMG_PROMISE 全漏）。
# 独立成表的原因：这类承诺是**即时**动作，对 _EXCLUDES_PAST_REF（过去指涉）
# **免疫**——「我以前画过一套明信片，等我找找看有没有存图」前半句的「以前」
# 不该豁免后半句的翻找承诺；否认/远期/疑问排除（_EXCLUDES）照常适用。
# 同句必须带照片语境词（相册/存图/照片/图…）防误伤「我找找那家店在哪」；
# 单字「图」带负向前瞻防「图书馆/图纸」。
_IMG_PROMISE_BROWSE = [re.compile(p, re.IGNORECASE) for p in (
    r"(?:等|让|讓)?\s*(?:我|人家)\s*(?:去)?\s*(?:找找|翻翻|找一?下|翻一?下)"
    r"[^。！？!?\n]{0,15}(?:相册|相簿|存图|存圖|照片|相片|自拍|"
    r"(?:图|圖)(?![书書纸紙标標案表鉴鑒]))",
    # en：browse-my-album 软承诺
    r"\blet\s+me\s+(?:look|dig|go)\s+through\s+my\s+(?:photos?|albums?|gallery|camera\s+roll)",
    r"\blet\s+me\s+(?:find|look\s+for|check\s+for)\s+(?:a|an|the|that|some)?\s*(?:photos?|pic(?:ture)?s?|selfies?)\b",
)]

# 视频通话承诺/应允（区别于"发你个视频"——那是媒体视频，走 _try_autosend_video 可兑现；
# 这里抓的是**通话**：可以视频/开视频/同你视频/视频聊/接你视频。宁漏勿误：
# 单说"视频"名词不算，必须带"可以/开/来/接/同你"等应允动词结构。
_VIDEOCALL_PROMISE = [re.compile(p, re.IGNORECASE) for p in (
    # zh/粤：可以视频(通话)、视频都开到、开个视频、同你/跟你/和你视频
    r"(?:可以|可與|可与|能|冇问题|冇問題|没问题|沒問題)\s*(?:同你|跟你|和你)?\s*(?:开|開)?\s*(?:视频|視頻|視像)\s*(?:通话|通話|聊|傾)?",
    r"(?:视频|視頻|視像)\s*(?:通话|通話)?\s*(?:都)?\s*(?:开到|開到|开得|開得|开着|開著)",
    r"(?:同你|跟你|和你|陪你)\s*(?:视频|視頻|視像)",
    r"(?:开|開|来|來|接)\s*(?:个|個)?\s*(?:视频|視頻|視像)\s*(?:通话|通話|聊|傾)?(?:啦|吧|喽|咯|啊)?",
    r"(?:视频|視頻|視像)\s*(?:聊|傾|见|見)\s*(?:一下|下)?",
    # en：video call promises
    r"\b(?:we\s+can|i\s+can|let(?:'|’)?s)\s+(?:do\s+a\s+)?video\s*(?:call|chat)",
    r"\bvideo\s*call\s+(?:me|you|works|is\s+fine|anytime)",
    r"\bi(?:'|’)?ll\s+video\s*(?:call|chat)\s+you",
)]

_VOICE_PROMISE = [re.compile(p, re.IGNORECASE) for p in (
    r"(?:发|發|传|傳)\s*你\s*(?:一?[条條段个個])?\s*(?:语音|語音)",
    r"给\s*你\s*(?:发|發|录|錄)\s*(?:一?[条條段个個])?\s*(?:语音|語音)",
    r"(?:我|人家)\s*(?:去)?\s*(?:录|錄)\s*(?:一?[条條段个個])?\s*(?:语音|語音)",
    r"等\s*我\s*(?:发|發|录|錄)\s*(?:语音|語音)",
    r"(?:语音|語音)\s*(?:说|說|讲|講)\s*给\s*你",
    # 2026-08-02 实录（A 线 TTS 失败空头支票）：「语音这就来～」——"在路上"式
    # 将发宣告，与图片轨「照片来啦/(photo) on the way」同语义；旧表只认
    # 「发/录+语音」动词结构全漏。窄口径：语音后必须紧跟即时副词+来/到。
    r"(?:语音|語音)\s*(?:这就|這就|马上|馬上|立刻|等下|一会儿?|一會兒|待会儿?|待會兒?)\s*(?:来|來|到)",
    r"\bi(?:'|’)?ll\s+(?:send|record)\s+(?:you\s+)?(?:a|an|one)?\s*voice",
    r"\blet\s+me\s+(?:send|record)\s+(?:you\s+)?(?:a|an)?\s*voice",
    r"\bsending\s+(?:you\s+)?a\s+voice",
    # ja / ko / es（宣告形语音承诺；宁漏勿误）
    r"(?:ボイス|音声|ボイスメッセージ)\s*(?:を)?\s*(?:送|おく)(?:る|り|っ)",
    r"(?:음성|보이스)[^\n]{0,6}보낼",
    r"\bte\s+mando\s+un\s+(?:audio|mensaje\s+de\s+voz)",
)]

# ── 完成/进行「断言」（claim）：声称照片/语音**已发或正在发**───────────────────
# 与 promise（将发）正交：promise 撤回改「先聊聊」，claim 是「已经发了」的谎——
# 本轮真发了媒体=真话（放行），没发=谎（撤回）。2026-07-29 对练实证：promise 词表
# 抓不到「这不就来了嘛/发到群里了/刚发的/你看看这张」这类完成态，是「说发没发」主因。
# claim 一律 gate 在 media_context（客户本轮在**索要**媒体）——否则「客户发图问好看吗
# →AI『这张真好看』」会被误判。事故场景全是客户要图/要语音，media_context 全覆盖。
_IMG_CLAIM_STRONG = [re.compile(p, re.IGNORECASE) for p in (
    # 照片/自拍/图 + (已经/刚/这就) + 发/传/给 + 你/了/过去/出去
    r"(?:照片|自拍|相片|美照|靓照|靚照|图片?|圖片?)\s*(?:已经|已經|刚|剛|这就|這就|就)?\s*"
    r"(?:发|發|传|傳|给|給)\s*(?:给|給)?\s*(?:你了?|过去了?|過去了?|出去了?|了|啦)",
    r"(?:发|發|传|傳)\s*(?:给|給)?\s*你\s*(?:了|啦|过去了|過去了)",   # 发你了/发给你了
    r"(?:发|發|传|傳)\s*(?:过去|過去|出去)\s*(?:了|啦)",             # 传过去了
    r"(?:照片|自拍|相片|图|圖)\s*(?:来|來)\s*(?:啦|了|咯|喽|囉)",     # 照片来啦
    r"刚\s*(?:才|剛)?\s*(?:拍|发|發|傳|传)\s*(?:的|了|好|完|好啦|好了|过去|過去)",  # 刚拍的/刚发的
    r"拍\s*好\s*(?:啦|了|咯)",                                       # 拍好啦（独立完成态）
    # 指示「这/那张」+ 看/怎么样/喜欢（暗示图已在对方手里）
    r"(?:这|這|那|上|前)\s*(?:一)?[张張][^。！？!?\n]{0,10}"
    r"(?:看|瞧|怎么样|怎麼樣|好看|喜欢|喜歡|如何|行不行|满意|滿意|中意)",
    r"(?:再)?\s*(?:仔细|仔細)?\s*看\s*(?:看|下|一下)?\s*(?:这|這|那)\s*(?:一)?[张張]",
    # 指示「这/那张」+ 系动词（2026-07-31 试聊实录：「这张是它刚来我家那天拍的」
    # 「那张是阿橘蹲在阳台晒太阳的侧影」——把不存在的图当作正在给对方看地描述。
    # media_context 门控下不误伤评论对方图（对方发图轮 media_context 被抑制）。
    r"(?:这|這|那)\s*(?:一)?[张張]\s*(?:是|就是|係)",
    # en
    r"\b(?:i\s+)?(?:just\s+)?sent\s+(?:you\s+)?(?:it|a|an|one|the|my|another)?\s*"
    r"(?:photo|pic(?:ture)?|selfie)?\b",
    r"\bhere(?:'|’)?s\s+(?:the|a|my|one)?\s*(?:photo|pic(?:ture)?|selfie)",
    r"\bthere\s+you\s+go\b", r"\bcheck\s+(?:it|this|that)\s+out\b",
    # #259 P-3（6SRA2B 14:49:51 / 14:50 实录漏网）：「Just took this one for
    # you—」「Ah, here it is—」——代词宾语的照片配文体，无 photo 名词，旧表全漏。
    # 只在媒体语境（客户在等图 / 5 分钟内刚真发过一张）下判，配文体离开图就是谎。
    r"\b(?:i\s+)?(?:just\s+)?(?:took|snapped|shot|grabbed)\s+(?:this|that|it|one|these|a\s+(?:quick\s+)?(?:one|pic|photo|selfie|shot))\b",
    r"\b(?:took|snapped|shot)\s+(?:this|that|it|one|a)\s*(?:one|pic|photo|selfie|shot)?\s+(?:just\s+)?for\s+(?:you|u|ya)\b",
    r"\bhere\s+(?:it\s+is|you\s+go|you\s+are)\b", r"\bthere\s+it\s+is\b",
    r"\b(?:it|this|that)\s+(?:should\s+be|is|'s)\s+(?:right\s+)?there\b", r"\bshould\s+be\s+there\b",
    # ja / ko
    r"送った(?:よ|ね)?|送りました", r"보냈(?:어|어요|습니다)",
    # 粤语完成态（啱啱/头先=刚刚 影咗=拍了 嘅=的）——「啱啱拍嘅」「影咗俾你」
    r"(?:啱啱|头先|頭先|刚先|剛先|头采|頭采)\s*(?:至|先)?\s*(?:影|拍)\s*(?:咗|嘅|好|返)",
    r"(?:影|拍)\s*咗\s*(?:张|張|返)?\s*(?:相|相片|自拍)?\s*(?:畀|俾)?\s*你?",
    r"(?:相|相片)\s*(?:已经|已經)?\s*(?:畀|俾)\s*(?:咗)?\s*你",
    r"(?:而家|啱啱|头先|頭先)\s*(?:先|至)?\s*(?:影|拍)\s*(?:嘅|㗎|架|咗)",
)]

_VOICE_CLAIM_STRONG = [re.compile(p, re.IGNORECASE) for p in (
    r"(?:语音|語音)\s*(?:已经|已經|刚|剛|这就|這就|就)?\s*(?:发|發|录|錄|传|傳)\s*(?:给|給)?\s*(?:你了?|过去了?|過去了?|了|啦)",
    r"(?:发|發|录|錄)\s*(?:给|給)?\s*你\s*(?:一?[条條段])?\s*(?:语音|語音)\s*(?:了|啦)",
    # 语序无关：录/发/传 (+了/好) (+一条) + 语音（含「录了条语音」「给你录了条语音」）
    r"(?:发|發|录|錄|传|傳)\s*了?\s*(?:一?[条條段个個])?\s*(?:语音|語音)",
    r"(?:语音|語音)\s*(?:来|來)\s*(?:啦|了)",
    r"\b(?:i\s+)?(?:just\s+)?sent\s+(?:you\s+)?(?:a\s+)?voice",
)]

# claim 专属排除面：只排「否认/疑问/远期/明确旧照」——**不排裸「那张/这张」**
# （promise 的 _EXCLUDES 把「那[张張]」当过去指涉，会把「刚发的那张」误放行，
#  正是审计点的核心矛盾）。只有带发送动词的「上次发/之前拍」才算真旧照引用。
_CLAIM_EXCLUDES = [re.compile(p, re.IGNORECASE) for p in (
    r"不能|不方便|没法|沒法|无法|無法|发不了|發不了|不发|不發|不给|不給|别发|別發|不会发|不會發|拍不了",
    r"\bcan(?:'|’)?t\b|\bcannot\b|\bunable\b|\bwon(?:'|’)?t\b",
    r"改天|下次|以后|以後|回头|回頭|有机会|有機會|哪天|下回|过几天|過幾天|周末|週末|明天|到时候|到時候|见面|見面",
    r"\bsomeday\b|\bnext\s+time\b|\btomorrow\b|\blater\s+this\b|\bwhen\s+we\s+meet\b",
    r"上次(?:发|發|拍|传|傳)|之前(?:发|發|拍|传|傳)|昨天(?:发|發|拍)|以前(?:发|發|拍)|前(?:几天|幾天)(?:发|發|拍)",
)]

# 弱完成态（不带媒体名词）——仅 media_context 时判（否则「消息来了/我来了」会误伤）
_MEDIA_DONE_WEAK = [re.compile(p, re.IGNORECASE) for p in (
    r"这不\s*就?\s*(?:来|來)\s*(?:了|啦)",                            # 这不就来了嘛
    # 光杆翻找承诺（2026-07-31 试聊实录：「哈哈那我找找看～」——句内无照片词，
    # 只有媒体语境才能定性；句尾锚定防吃掉「我找找看那家餐厅」这类带宾语的句子）
    r"(?:我|人家)\s*(?:就)?\s*(?:去)?\s*(?:找找|翻翻)\s*(?:看|下|一下)?\s*[哈啦哦呀嘛]?\s*$",
    r"(?:发|發|传|傳)\s*(?:到|去|在)\s*(?:群|群里|群裡|群組|群组)",   # 发到群里（私聊错乱强信号）
    r"你\s*(?:再)?\s*看\s*(?:看|下|一下)?\s*(?:合|喜|中意|满意|滿意|好不好|怎么样|怎麼樣|漂不漂亮|美不美)",
    r"(?:已经|已經|刚|剛|这就|這就|就)\s*(?:发|發|传|傳)\s*(?:了|啦|出去|过去|過去)(?![什么么问])",
    r"给\s*你\s*(?:发|發|传|傳)\s*(?:了|啦|过去了|過去了)",
    r"\b(?:i\s+)?(?:just\s+)?sent\s+it\b|\byou\s+can\s+see\s+it\b|\bit(?:'|’)?s\s+there\b",
    # #259 P-3：「let me send it (now/over)」代词宾语的将发句（promise 表要求 photo 名词）
    r"\blet\s+me\s+(?:just\s+)?send\s+(?:it|that|this|one)\s+(?:now|over|to\s+you|right\s+now|real\s+quick)\b",
)]

# ── 客户是否在「索要」媒体（media_context 判据；A/B 线共用）────────────────────
_WANTS_IMG = [re.compile(p, re.IGNORECASE) for p in (
    r"(?:发|發|传|傳|来|來|給|给|拍)\s*(?:张|個|个|張|条|一下)?\s*(?:你的?)?\s*(?:照片|自拍|相片|真人|靓照|靚照|图|圖|样子|樣子)",
    r"看\s*(?:看|下|一下)?\s*(?:你|你的?)\s*(?:照片|自拍|真人|样子|樣子|长啥样|長啥樣|长什么样)",
    # 有没有/想要/给我 + 照片；「店里的照片」「海边的照片」这类点名索要
    r"(?:有没有|有無|有冇|想要|想看|要看|給我|给我|发我|發我)\s*(?:.{0,10})?(?:照片|自拍|相片|靓照|靚照)",
    r"(?:照片|自拍|相片)\s*(?:呢|吗|嗎|呀|啊|嘛)?\s*[?？]?\s*$",
    # 裸「照」索要（实施69 实录漏网：「拍个照我看看」「拍照烤串的给我看看」——
    # 名词表只有照片/自拍/图，量词+「照」与「拍照X给我看」两种口语语序全漏，
    # 而这是 claim 门控（media_context）的判据，漏＝谎言门全程不开）。
    r"拍\s*(?:个|個|张|張|一张|一張)\s*照",
    r"拍照?[^，。！？!?\n]{0,10}(?:给|給|俾)\s*我\s*(?:看|睇)",
    # 裸「图」索要（2026-07-31 试聊实录：「有图嘛」——上面各条都要求
    # 照片/自拍全词，「图」单字只在句式收口时认，防「地图/图书馆」误伤）
    r"(?:有没有|有沒有|有冇|有無|有)\s*(?:图|圖)\s*(?:吗|嗎|嘛|呀|啊|呢)?\s*[?？]?\s*$",
    r"^\s*(?:图|圖)\s*(?:呢|咧|勒)\s*[?？]?\s*$",
    r"你(?:长|長)\s*(?:啥|什么|甚麼)\s*样",
    r"是\s*(?:不是|你)\s*本人|真人(?:吗|嗎|吧)|是真人",
    r"\b(?:send|show|got|have)\s+(?:me\s+)?(?:a\s+|your\s+)?(?:photo|pic(?:ture)?|selfie|face)",
    r"\bwhat\s+do\s+you\s+look\s+like\b|\breal\s+person\b",
    # 粤语索要（影相=拍照 睇=看 畀/俾我睇=给我看）
    r"(?:影|拍)\s*(?:张|張)?\s*(?:真人)?\s*(?:相|相片|自拍)",
    r"(?:畀|俾)\s*我\s*(?:睇|睇下|睇吓)|睇\s*(?:下|吓)?\s*你\s*(?:嘅)?\s*(?:相|样|樣|真身)",
    r"真身\s*系?\s*咪|係咪真人|系咪真人",
)]
_WANTS_VOICE = [re.compile(p, re.IGNORECASE) for p in (
    r"(?:发|發|录|錄|来|來)\s*(?:条|個|个|段)?\s*(?:语音|語音)",
    r"想\s*听\s*(?:听|下)?\s*(?:你的?)?\s*(?:声音|聲音|嗓音)",
    r"(?:语音|語音)\s*(?:呗|吧|来|來|我听|說|说)",
    r"唱\s*(?:首|个|個|一|两|兩)?\s*(?:歌|首歌)|唱\s*(?:给|給|来|來).{0,4}(?:听|聽)|给我唱|給我唱|唱两句|唱兩句",
    r"说\s*句?\s*(?:话|話)\s*(?:我)?\s*听|說\s*句?\s*(?:话|話)",
    r"\b(?:send|record)\s+(?:me\s+)?(?:a\s+)?voice|\bsing\b|\bhear\s+your\s+voice",
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
)]

# 过去指涉排除（独立组，2026-07-31 拆分）：保护「上次发你的那张」类**描述**不被
# 当承诺剥掉。只豁免 _IMG_PROMISE/_VOICE_PROMISE（拍/发类），**不豁免**
# _IMG_PROMISE_BROWSE（翻找类是即时动作，混合句「以前画过…等我找找」照抓）。
_EXCLUDES_PAST_REF = [re.compile(p, re.IGNORECASE) for p in (
    r"上次|之前|那[张張]|昨天|前几天|前幾天|以前|刚才发|剛才發|\blast\s+time\b|\bearlier\b|\bthat\s+(?:photo|pic)\b",
    r"この前|さっき送|昨日|아까\s*보낸|지난번",
)]

_QUESTION_TAIL_RE = re.compile(r"[?？]\s*$")


def _sentence_is_promise(sent: str) -> str:
    """单句判定：返回 'image'/'voice'/'video_call'/''。疑问句/排除面命中一律不算。

    排除面分两组：否认/远期/疑问（_EXCLUDES）对一切承诺模式适用；过去指涉
    （_EXCLUDES_PAST_REF）只豁免「拍/发」类——翻找式（_IMG_PROMISE_BROWSE）
    是即时动作，混合句「我以前画过明信片，等我找找看有没有存图」里的「以前」
    不该连带豁免后半句的翻找承诺（2026-07-31 试聊实录漏网根因）。
    """
    s = str(sent or "").strip()
    if not s:
        return ""
    if _QUESTION_TAIL_RE.search(s):
        return ""
    for ex in _EXCLUDES:
        if ex.search(s):
            return ""
    for rx in _IMG_PROMISE_BROWSE:
        if rx.search(s):
            return KIND_IMAGE
    for ex in _EXCLUDES_PAST_REF:
        if ex.search(s):
            return ""
    for rx in _IMG_PROMISE:
        if rx.search(s):
            return KIND_IMAGE
    for rx in _VIDEOCALL_PROMISE:
        if rx.search(s):
            return KIND_VIDEOCALL
    for rx in _VOICE_PROMISE:
        if rx.search(s):
            return KIND_VOICE
    return ""


def detect_media_promise(text: str) -> str:
    """出站文本是否承诺「即刻发照片/语音/视频通话」。
    返回 'image'/'voice'/'video_call'/''（image 优先——它有真实兑现路径）。

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


def promised_scene(text: str) -> str:
    """承诺发图句里点名的**场景**（纯函数）：「等我拍张海边的发你」→ beach 场景短语。

    与客户点名共用同一张场景词表（``companion_selfie.extract_requested_scene``）——
    兑现层据此把承诺场景升为硬要求（相册场景类匹配 / 生成带场景），修
    「承诺海景、兑现成车内自拍」实录事故。只扫**承诺句本身**（同条里别的句子
    提到的地点不算承诺内容）；没点名/词表外返回空串（兑现不限场景）。
    """
    for sent in _sentences(text):
        if _sentence_is_promise(sent) != KIND_IMAGE:
            continue
        try:
            from src.ai.companion_selfie import extract_requested_scene
            sc = extract_requested_scene(sent)
        except Exception:
            sc = ""
        if sc:
            return sc
    return ""


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


# ── 完成/进行「断言」检测（claim；本轮无媒体时即谎言）───────────────────────────
# 客户自述动作前置（实施69）：「我去拍个照」「我先拍张照」是客户叙述自己
# 要拍照，不是向 AI 索图——新收的「拍X照」结构若无此护栏会把它当索要。
_OWN_ACTION_RE = re.compile(
    r"^\s*(?:我|俺|人家)\s*(?:去|要|来|來|先|这就|這就|马上|馬上|刚|剛|已经|已經)?\s*拍")


def wants_media(peer_text: str) -> str:
    """客户本条是否在**索要**照片/语音/唱歌 → 'image'/'voice'/''（纯函数）。

    这是 claim 检测的 media_context 判据（A/B 线共用）：只有客户在要媒体时，
    才把 AI 的「这不就来了嘛/你看看这张」当完成断言判——否则「客户发图问好看吗
    →AI『这张真好看』」会被误伤（那是评论对方的图，不是声称自己发了）。
    """
    s = str(peer_text or "")
    if not s.strip():
        return ""
    if _OWN_ACTION_RE.match(s):
        return ""
    for rx in _WANTS_IMG:
        if rx.search(s):
            return KIND_IMAGE
    for rx in _WANTS_VOICE:
        if rx.search(s):
            return KIND_VOICE
    return ""


def _sentence_claim_kind(sent: str, *, media_context: bool) -> str:
    """单句是否「声称已发/正在发」媒体。返回 'image'/'voice'/''。

    strong（带媒体名词/指示词）：media_context 时判。
    weak（宽泛完成词）：仅 media_context 时判（无语境时「消息来了」会误伤）。
    疑问句/远期/否认（EXCLUDES）一律不算——「照片发你了吗？」是问不是断言。
    """
    s = str(sent or "").strip()
    if not s or not media_context:
        return ""
    if _QUESTION_TAIL_RE.search(s):
        return ""
    for ex in _CLAIM_EXCLUDES:
        if ex.search(s):
            return ""
    # voice 先于 image：「语音发你了」的「发你了」也命中 IMG_STRONG，须先判语音轨
    for rx in _VOICE_CLAIM_STRONG:
        if rx.search(s):
            return KIND_VOICE
    for rx in _IMG_CLAIM_STRONG:
        if rx.search(s):
            return KIND_IMAGE
    for rx in _MEDIA_DONE_WEAK:
        if rx.search(s):
            return KIND_IMAGE  # 弱完成态默认归 image（最常见；语音有独立 strong 轨）
    return ""


def detect_media_claim(text: str, *, media_context: bool = False) -> str:
    """出站文本是否**声称**媒体已发/正在发（区别于 detect_media_promise 的「将发」）。

    返回 'image'/'voice'/''（image 优先——它有兑现路径可把谎变真）。
    ``media_context``＝客户本轮在索要媒体（见 ``wants_media``），False 直接返回 ''。
    """
    if not media_context:
        return ""
    found = ""
    for sent in _sentences(text):
        k = _sentence_claim_kind(sent, media_context=True)
        if k == KIND_IMAGE:
            return KIND_IMAGE
        if k and not found:
            found = k
    return found


def strip_media_claims(text: str, *, media_context: bool = False) -> str:
    """句级剥离「声称已发」的断言句（与 strip_media_promises 对称）。

    只在「本轮无媒体真发 + 客户在要媒体」时调用；media_context=False 原样返回。
    """
    raw = str(text or "")
    if not raw.strip() or not media_context:
        return raw
    parts = _SENT_SPLIT_RE.split(raw)
    out: List[str] = []
    i = 0
    while i < len(parts):
        seg = parts[i]
        delim = parts[i + 1] if i + 1 < len(parts) else ""
        if seg.strip() and _sentence_claim_kind(seg, media_context=True):
            i += 2
            continue
        out.append(seg)
        if delim:
            out.append(delim)
        i += 2
    res = "".join(out).strip()
    if res and not re.search(r"[\w\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", res):
        return ""
    return res


# ── 「已发」假声明（sent-claim，#171 2026-09-05）────────────────────────────────
# 实录（WhatsApp Mizuki→John，88MP86 22:26/22:27）：「Oh, sorry — I just sent it,
# you should have it now.」→ 客户「I never got it」→「Hmm, that's weird — let me
# try sending it again for you.」全程零 send_media，真图 23 分钟后才由另一句入站
# 触发。两道既有防线为何都没开口：``detect_media_claim`` 吃 media_context 门控，
# 门只在客户**近几条**用媒体名词索图时开（John 的原话没进词表，「I never got it」
# 也不含名词）；promise 词表要求 photo/pic 名词，「sending it again」是代词宾语。
#
# 本组**不吃 media_context**：「我刚发了/你该收到了/我再发一次/收到了吗」这类
# 第一人称完成态本身就是强断言，真伪交调用方对照**近 N 分钟媒体真发账本**判
# （B 线 inbox 出站 media_type / A 线 ``_media_sent_log``，见 ``media_pending.
# media_sent_within``）——账本比语境猜测可靠：近窗内真发过＝真话放行，没发＝谎。
# 排除面（宁漏勿误）：否定（didn't/never sent、没发/还没发）、方向相反（did you
# send / 你发了吗、sent it to my mom）、非媒体宾语（money/link/address、红包/快递/
# 地址…——那类谎不归发图链管，拉进来会误发自拍）、远过去（yesterday/上周——
# 30 分钟账本判不了）。刻意**不**跳过问号句：「did you get the photo?」正是
# 断言形态之一，方向相反的问句由排除面而不是问号处理。
_SENT_CLAIM_IMG = [re.compile(p, re.IGNORECASE) for p in (
    # en：I (just|already) sent it / sent you the photo / sent one over
    r"\bi(?:'ve|’ve|\s+have|'d|’d|\s+had)?\s+(?:just\s+|already\s+|literally\s+)?(?:re-?)?sent\s+"
    r"(?:you\s+|u\s+)?(?:it|that|those|one|"
    r"(?:the|a|an|my|another|some|those|these|two|2)\s+"
    r"(?:photos?|pics?|pictures?|images?|selfies?|shots?))\b",
    # en：(already|just) sent it（省主语；「you/he sent」由排除面挡）
    r"\b(?:just|already)\s+sent\s+(?:it|that|those|one|"
    r"(?:the|a|an|my|another)\s+(?:photos?|pics?|pictures?|images?|selfies?))\b",
    # en：you should have it (now|by now) / you should (be able to) see it
    r"\b(?:you|u)\s+should\s+(?:have\s+(?:gotten|received|got)?|(?:be\s+able\s+to\s+)?see|be\s+getting)\s*"
    r"(?:it|them|that|the\s+(?:photos?|pics?|pictures?|images?|selfies?)|"
    r"my\s+(?:photos?|pics?|pictures?|selfies?))\b",
    # en：it should be there / it should have gone through / it's in your chat
    r"\bit\s+should\s+(?:be\s+(?:there|in\s+your\s+(?:inbox|chat|messages|dms?|whatsapp|phone))|"
    r"have\s+(?:come|gone)\s+through)\b",
    r"\bit(?:'s|’s|\s+is)\s+(?:already\s+)?in\s+your\s+(?:inbox|chat|messages|dms?|whatsapp|phone)\b",
    # en：(let me) (try) send(ing) it again / one more time —— 含「上一次已发」预设
    r"\b(?:let\s+me\s+|i(?:'ll|’ll|\s+will|'m\s+gonna|’m\s+gonna|\s+am\s+gonna|'m\s+going\s+to|’m\s+going\s+to|\s+am\s+going\s+to)\s+|i(?:'m|’m|\s+am)\s+)?"
    r"(?:just\s+)?(?:try\s+(?:and\s+|to\s+)?)?(?:re-?send(?:ing)?|send(?:ing)?)\s+"
    r"(?:it|that|them|those|the\s+(?:photos?|pics?|pictures?|images?|selfies?))\s+"
    r"(?:again|one\s+more\s+time|once\s+more|over\s+again|a\s+second\s+time)\b",
    # en：let me resend / I'll resend（第一人称 resend 动词本身预设「发过」）
    r"\b(?:let\s+me|i(?:'ll|’ll|\s+will|'m\s+gonna|’m\s+gonna|\s+am\s+gonna|'m\s+going\s+to|’m\s+going\s+to|\s+can|\s+could))\s+"
    r"(?:just\s+)?(?:try\s+(?:and\s+|to\s+)?)?re-?send(?:ing)?\b",
    r"\bre-?sending\s+(?:it|that|them|those)\s+(?:now|right\s+now|to\s+you)\b",
    # en：did you get it yet? / did you get the photo? / did it go through?
    r"\b(?:did|didn(?:'|’)?t|have|haven(?:'|’)?t)\s+(?:you|u|ya)\s+(?:get|got|receive|received|see|seen)\s+"
    r"(?:(?:it|them|that|those)\s+(?:yet|already)|(?:the|my)\s+(?:photos?|pics?|pictures?|images?|selfies?)|"
    r"what\s+i\s+(?:just\s+)?sent)\b",
    r"\bdid\s+(?:it|they|the\s+(?:photos?|pics?|pictures?|images?))\s+(?:go|come|get)\s+through\b",
    # en：sending it now / over（代词宾语进行态；promise 词表要求 photo 名词）
    r"\bsending\s+(?:it|them|that|those|one)\s+(?:now|right\s+now|over|your\s+way|to\s+you)\b",
    # #259 P-3（6SRA2B 14:50 / 14:51 实录）：「I thought it went through」「maybe it
    # took a moment to load on your end」「check your phone」——传输借口 / 让对方查收
    # 都预设「我已经发了」，与 sent-claim 同真伪判据（近窗真发过才算真话）。
    r"\b(?:it|that|this|the\s+(?:photo|pic\w*|image|selfie))\s+(?:just\s+|already\s+|probably\s+|definitely\s+)?(?:went|got|came)\s+through\b",
    r"\b(?:took|takes|taking|might\s+take|may\s+take)\s+(?:a\s+)?(?:moment|while|sec(?:ond)?|minute|bit|little)\b[^.!?\n]{0,12}\bto\s+load\b",
    r"\b(?:still\s+)?load(?:ing|ed)?\s+on\s+your\s+(?:end|side|phone)\b",
    r"\bcheck\s+(?:your|ur)\s+(?:phone|chat|inbox|whatsapp|dms?|messages|gallery|notifications?)\b",
    # zh：看手机（让对方查收＝自称已发）；「别看/少看/老看手机」由排除面挡
    r"(?:去|快|快去|你)?\s*看\s*(?:看|下|一下)?\s*(?:你的?)?\s*手机",
    # zh：已经/刚/刚刚/刚才 + (给你)发/传 + 你/过去/出去 (+了)
    r"(?:已经|已經|刚刚|剛剛|刚才|剛才|刚|剛|这不|這不)\s*(?:就)?\s*(?:给|給)?\s*你?\s*"
    r"(?:发|發|传|傳)\s*(?:给|給)?\s*(?:你|过去|過去|出去)\s*(?:了|啦)?",
    # zh：发给你了 / 发你了 / 发过去了 / 传出去了 / 给你发(过去)了
    r"(?:发|發|传|傳)\s*(?:给|給)\s*你\s*(?:了|啦)|(?:发|發|传|傳)\s*你\s*(?:了|啦)",
    r"(?:发|發|传|傳)\s*(?:过去|過去|出去)\s*(?:了|啦)",
    r"(?:给|給)\s*你\s*(?:发|發|传|傳)\s*(?:过去|過去)?\s*(?:了|啦)",
    # zh：我再发一次/一遍、重发一下、再发给你（补发承诺预设「发过」）
    r"(?:再|重新|重)\s*(?:发|發|传|傳)\s*(?:一次|一遍|一下|一回|给你|給你|过去|過去|你)",
    # zh：你收到了吗 / 收到没（出站问对方是否收到＝自称已发）
    r"(?:你)?\s*(?:收到|收得到)\s*(?:了)?\s*(?:吗|嗎|没|沒|没有|沒有|未|不)",
)]

_SENT_CLAIM_VOICE = [re.compile(p, re.IGNORECASE) for p in (
    r"\bi(?:'ve|’ve|\s+have)?\s+(?:just\s+|already\s+)?sent\s+(?:you\s+|u\s+)?(?:a|the|my|another)\s+"
    r"(?:voice\s*(?:note|message|memo|msg)|audio(?:\s+message)?|vn|recording)\b",
    r"\b(?:you|u)\s+should\s+(?:have|hear|see)\s+(?:the|my)\s+"
    r"(?:voice\s*(?:note|message|memo)|audio|recording)\b",
    r"(?:语音|語音)\s*(?:已经|已經|刚|剛|刚刚|剛剛)?\s*(?:发|發|传|傳)\s*(?:给|給)?\s*"
    r"(?:你|过去|過去|出去)\s*(?:了|啦)?",
    r"(?:已经|已經|刚刚|剛剛|刚才|剛才|刚|剛)\s*(?:给|給)?\s*你?\s*(?:发|發|录|錄)\s*了?\s*"
    r"(?:一?[条條段个個])?\s*(?:语音|語音)",
    r"(?:再|重新|重)\s*(?:发|發)\s*(?:一?[条條段个個]|一次|一遍)?\s*(?:语音|語音)",
)]

_SENT_CLAIM_EXCLUDES = [re.compile(p, re.IGNORECASE) for p in (
    # 否定：没发/还没发/发不出去/不是我发的
    r"没\s*(?:有)?\s*(?:发|發|传|傳)|沒\s*(?:有)?\s*(?:发|發|传|傳)|"
    r"还没\s*(?:发|發)|還沒\s*(?:发|發)|不是\s*我\s*(?:发|發)|"
    r"(?:发|發|传|傳)\s*不\s*(?:出|了|上)|(?:发|發)\s*失败|(?:发|發)\s*失敗",
    r"\b(?:didn(?:'|’)?t|did\s+not|never|haven(?:'|’)?t|have\s+not|hasn(?:'|’)?t|"
    r"couldn(?:'|’)?t|could\s+not|can(?:'|’)?t|cannot|won(?:'|’)?t|forgot\s+to|"
    r"failed\s+to|wasn(?:'|’)?t\s+able\s+to|not\s+able\s+to)\s+(?:\w+\s+)?(?:send|sent|re-?send)\b",
    # 方向相反：问/让对方发；「你/他刚发」是别人发的
    r"\b(?:can|could|would|will|did|do|have|are|pls|please)\s+(?:you|u|ya)\s+(?:please\s+)?"
    r"(?:re-?send|send|forward|share|resent)\b",
    r"\b(?:you|u|he|she|they|we)\s+(?:just\s+|already\s+)?(?:sent|resent)\b",
    r"你\s*(?:再)?\s*(?:发|發|传|傳)\s*(?:一次|一遍|一下|过来|過來|给我|給我|了\s*(?:吗|嗎|没|沒))|"
    r"(?:你|他|她|他们|他們|你们|你們)\s*(?:刚|剛|已经|已經)?\s*(?:发|發|传|傳)\s*(?:给|給)?\s*我",
    # 第三方去向（发给别人不是发给对方）
    r"\bsent\s+(?:it|them|that|those|one|the\s+\w+|a\s+\w+)\s+to\s+(?!(?:you|u|ya)\b)",
    r"(?:发|發|传|傳)\s*(?:给|給)\s*(?!你)(?:我|他|她|它|老板|老闆|朋友|同事|家人|妈|媽|爸|群)",
    # 「别看/少看/老看手机」是劝对方放下手机，不是让对方查收（#259 P-3 看手机条）
    r"(?:别|別|不要|少|老|总|總|一直|天天|光)\s*看\s*(?:看|下|一下)?\s*(?:你的?)?\s*手机",
    # 非媒体宾语（那类谎不归发图链管，拉进来会误发自拍）
    r"\b(?:money|payment|cash|deposit|transfer|wire|invoice|bill|receipt|link|url|address|"
    r"(?:phone\s+)?number|email|e-mail|mail|gift|present|package|parcel|order|file|document|"
    r"doc|code|otp|password|request|invite|invitation|location|pin|contact|message|text|msg|"
    r"dm|letter|resume|cv|form|application|details|info|information|schedule|itinerary|"
    r"tickets?|flowers)\b",
    r"红包|紅包|转账|轉賬|转帐|轉帳|付款|汇款|匯款|钱|錢|链接|鏈接|连结|連結|网址|網址|地址|"
    r"定位|位置|号码|號碼|手机号|手機號|邮件|郵件|邮箱|郵箱|消息|信息|讯息|訊息|短信|简讯|"
    r"簡訊|留言|文件|文档|文檔|资料|資料|名片|验证码|驗證碼|礼物|禮物|快递|快遞|包裹|订单|"
    r"訂單|合同|合約|发票|發票|简历|簡歷|表格|申请|申請|邀请|邀請|朋友圈|动态|動態|微博|"
    r"工资|工資|奖金|獎金",
    # 远过去（近窗账本判不了，真伪无法即时证实）
    r"\byesterday\b|\blast\s+(?:week|night|month|year|time)\b|\b(?:days?|weeks?|months?)\s+ago\b|"
    r"\bthe\s+other\s+day\b|\bearlier\s+(?:this\s+week|today)\b|\bthis\s+morning\b",
    r"昨天|昨晚|前天|上周|上週|上个月|上個月|上次|之前|前几天|前幾天|今早|上午",
)]


def _sentence_sent_claim_kind(sent: str) -> str:
    """单句是否「声称已发/要补发」媒体（不吃 media_context）。返回 'image'/'voice'/''。"""
    s = str(sent or "").strip()
    if not s:
        return ""
    for ex in _SENT_CLAIM_EXCLUDES:
        if ex.search(s):
            return ""
    for rx in _SENT_CLAIM_VOICE:
        if rx.search(s):
            return KIND_VOICE
    for rx in _SENT_CLAIM_IMG:
        if rx.search(s):
            return KIND_IMAGE
    return ""


def detect_sent_claim(text: str) -> str:
    """出站文本是否含「已经发了 / 你该收到了 / 我再发一次 / 收到了吗」式**过去时
    假声明**（#171）。返回 'image'/'voice'/''（image 优先——有兑现路径）。

    与 :func:`detect_media_claim` 的分工：那个抓「这不就来了嘛/你看看这张」类
    完成态但必须由客户索图语境开门；本函数抓的形态本身就是强断言，**不吃语境**，
    真伪由调用方对照近窗媒体真发账本判定（真发过＝真话，绝不剥）。
    """
    found = ""
    for sent in _sentences(text):
        k = _sentence_sent_claim_kind(sent)
        if k == KIND_IMAGE:
            return KIND_IMAGE
        if k and not found:
            found = k
    return found


def strip_sent_claims(text: str) -> str:
    """句级剥离「已发假声明」句（与 strip_media_claims 对称；剥空返回空串由调用方兜底）。

    只在「命中 sent-claim 且近窗无媒体真发」时调用；正常文本原样返回。
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
        if seg.strip() and _sentence_sent_claim_kind(seg):
            i += 2
            continue
        out.append(seg)
        if delim:
            out.append(delim)
        i += 2
    res = "".join(out).strip()
    if res and not re.search(r"[\w\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", res):
        return ""
    return res


def build_photo_unsent_rewrite_instruction(text: str) -> str:
    """#259 P-3 A 段：模型发了 ``send_photo``/[PHOTO] 请求、正文按「图已发」写，但
    图这一轮**没能发出**（无匹配图 / 发送失败）→ 把这个事实回喂模型，重生一句
    **不提照片、不承诺、不解释**的自然回复。只输出正文。"""
    t = str(text or "").strip()[:400]
    return (
        "下面这条聊天消息是配着一张照片写的（把照片当作已经发出/刚拍好来写），"
        "但实际上这一轮照片**没有发出去**，对方不会收到任何图片。\n"
        "请改写这条消息：删掉所有关于这张照片的措辞（「刚拍的 / 给你看 / here it is /"
        " took this for you / 你看这张」等），也不要改口说「等我拍 / 稍后发」或"
        "「没发出去 / 传不了」——直接当作这一轮没提照片，用同样的语言和语气自然地"
        "接着对方的话聊（可以回应对方说的内容、反问或轻松带过），长度与原消息相近。"
        "不要道歉、不要解释系统原因、不要说自己是 AI。\n"
        "只输出改写后的消息正文，不要引号。\n"
        f"原消息：「{t}」"
    )


def build_promise_rewrite_instruction(
    text: str, kind: str = KIND_IMAGE, *, sent_claim: bool = False,
) -> str:
    """LLM 撤回重写指令（首选路径；任意语言可靠）。只输出改写后的消息正文。

    ``sent_claim=True``（#171）＝原文是「已经发了/你该收到了/我再发一次」式假声明：
    改写方向不是「岔开话题卖关子」而是**如实说这边这会儿发不出去、稍后补**——
    客户刚说「我没收到」，再卖关子等于二次欺骗。
    """
    t = str(text or "").strip()[:400]
    if sent_claim and kind in (KIND_IMAGE, KIND_VOICE):
        what = "语音" if kind == KIND_VOICE else "照片"
        return (
            f"下面这条聊天消息声称{what}已经发出去了/让对方查收/说要再发一次，"
            f"但实际上这一轮并没有发出任何{what}。\n"
            f"请改写这条消息：删掉所有「刚发了/已经发了/你应该收到了/我再发一次/"
            f"收到了吗」这类关于{what}的已发断言和补发承诺，改成如实、轻描淡写地说"
            f"这边{what}这会儿传不出去、稍后再补（不要编造别的借口、不要道歉连篇、"
            "不要解释系统原因、不要说自己是 AI）；其余内容、语言、语气、长度尽量"
            "保持原样，用与原消息相同的语言输出。\n"
            "只输出改写后的消息正文，不要引号。\n"
            f"原消息：「{t}」"
        )
    if kind == KIND_VIDEOCALL:
        # 视频通话没有兑现路径：承诺=谎言。改写方向是"婉拒但不冷场"。
        return (
            "下面这条聊天消息答应了可以视频通话，但实际上不能视频通话。\n"
            "请改写这条消息：删掉所有「可以视频/开视频/跟你视频」这类应允，"
            "换成自然的婉拒（比如这边不方便开视频、先文字/语音聊），"
            "其余内容、语言、语气尽量保持原样；不要道歉连篇、不要解释系统原因、"
            "不要说自己是 AI。\n"
            "只输出改写后的消息正文，不要引号。\n"
            f"原消息：「{t}」"
        )
    what = "语音" if kind == KIND_VOICE else "照片"
    return (
        f"下面这条聊天消息里，答应要发{what}、或声称{what}已经发了/发过去了/"
        f"让对方查收，但{what}这一轮实际并没有发出去。\n"
        f"请改写这条消息：删掉所有「要发/正在发/等我拍/来了/已经发了/发过去了/"
        f"你看看这张/发到群里」这类关于{what}的承诺、暗示或**已发断言**，"
        "其余内容、语言、语气、长度尽量保持原样；"
        "如果整条消息都在说这件事，就用同样的语言和语气写一句自然地岔开话题的话"
        "（比如先聊聊天、卖个关子），不要道歉连篇、不要解释系统原因、不要说自己是 AI。\n"
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
    KIND_VIDEOCALL: {
        "zh": "视频就先不啦，我这边不太方便～先这样聊嘛😊",
        "ja": "ビデオ通話はまた今度ね、今はこのままお話しよ😊",
        "ko": "영상통화는 다음에~ 지금은 이렇게 얘기하자😊",
        "en": "no video for now, it's not convenient on my side~ let's keep chatting like this 😊",
    },
}

# 「已发假声明」被整句剥空时的兜底（#171）：客户刚说「没收到」，再卖关子＝二次
# 欺骗——这里如实说「这边没发出去、这会儿传不了、稍后补」（远期口径，不做即时
# 新承诺；文案已过 detect_media_promise / detect_sent_claim 双检不回环）。
_SENT_CLAIM_DEFLECTIONS = {
    KIND_IMAGE: {
        "zh": "呃，照片好像没发出去…我这边这会儿传不了，晚点补给你哈",
        "ja": "あれ、写真うまく送れてなかったみたい…今ちょっと送れないから、あとで送るね",
        "ko": "어라, 사진이 제대로 안 갔나 봐… 지금은 잘 안 보내져서 조금 뒤에 다시 보내줄게",
        "en": "ugh, looks like the photo didn't actually go through on my end… "
              "it won't send right now, I'll make it up to you a bit later",
    },
    KIND_VOICE: {
        "zh": "呃，语音好像没发出去…这会儿录不了，先打字聊哈",
        "ja": "あれ、ボイスが送れてなかったみたい…今は録れないから、まずメッセージで話そ",
        "ko": "어라, 음성이 안 갔나 봐… 지금은 녹음이 안 돼서 일단 문자로 얘기하자",
        "en": "ugh, looks like the voice note didn't go through… "
              "I can't record right now, let's just text for a bit",
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


def deflection_line(
    sample_text: str, kind: str = KIND_IMAGE, *, sent_claim: bool = False,
) -> str:
    """整句被剥空后的语言对齐兜底：轻巧岔开话题、不否认能力、不做新承诺。

    ``sent_claim=True``（#171）→ 改用「这边没发出去、稍后补」的如实口径。"""
    if sent_claim and kind in _SENT_CLAIM_DEFLECTIONS:
        table = _SENT_CLAIM_DEFLECTIONS[kind]
        return table.get(_script_lang(sample_text), table.get("en", ""))
    table = _DEFLECTIONS.get(kind if kind in _DEFLECTIONS else KIND_IMAGE, {})
    return table.get(_script_lang(sample_text), table.get("en", ""))


# ── #259 P-3 固定动作模板（不经 LLM；文案已过本模块全部检测器，测试守不回环）────
# D 段：人设无发图能力 / 相册空 → 承诺句改写为**诚实拒绝**（不是「卖关子」——
# 卖关子暗示「以后会发」，能力关的人设永远发不出，那仍是空头支票）。
_HONEST_NO_PHOTO = {
    "zh": "这会儿发不了照片呢，先陪我聊聊嘛～",
    "ja": "今は写真送れないんだ〜先にもっと話そ？",
    "ko": "지금은 사진 못 보내~ 일단 얘기 더 하자😊",
    "en": "I can't send a photo right now~ let's just chat for a bit 😊",
}
# C 段②：客户说「没收到」而本会话**从未真发过**媒体 → 强制实话：承认没发出去、
# 说自己去看一下。禁「加载慢 / 再试一次 / 网络不好」三类传输借口（实录 14:50 /
# 14:51 两句谎话正是这三类）。
_LIE_CAUGHT_HONEST = {
    "zh": "呃，刚才的照片没发出去…我这边查一下，别急",
    "ja": "あれ、さっきの写真ちゃんと送れてなかったみたい…こっちで確認してみるね",
    "ko": "어라, 아까 사진이 제대로 안 갔나 봐… 내가 여기서 확인해볼게",
    "en": "hmm, looks like the photo didn't actually go out on my end. "
          "let me check what happened here",
}
# C 段①：客户说「没收到」且本会话最近真发过一张 → 自动**重发同一张**，配这一句
# 短话（随图发出，不经守卫——它绑着真实的 send_media 回执）。
_LIE_CAUGHT_RESEND_CAPTION = {
    "zh": "咦，没收到吗？我再发一次这张～",
    "ja": "あれ、届いてなかった？もう一回送るね〜",
    "ko": "어? 안 갔어? 다시 한 번 보낼게~",
    "en": "oh? didn't get it? here, sending this one again~",
}


def honest_no_photo_line(sample_text: str) -> str:
    """D 段：能力关 / 无图可发时的诚实拒绝（按 sample 文字系统取语言）。"""
    return _HONEST_NO_PHOTO.get(_script_lang(sample_text), _HONEST_NO_PHOTO["en"])


def lie_caught_honest_line(sample_text: str) -> str:
    """C 段②：从未真发过却被问「没收到」→ 强制实话模板。"""
    return _LIE_CAUGHT_HONEST.get(_script_lang(sample_text), _LIE_CAUGHT_HONEST["en"])


def lie_caught_resend_caption(sample_text: str) -> str:
    """C 段①：重发上一张时随图的短配文。"""
    return _LIE_CAUGHT_RESEND_CAPTION.get(
        _script_lang(sample_text), _LIE_CAUGHT_RESEND_CAPTION["en"])


def detect_photo_caption_claim(text: str) -> str:
    """「照片配文体」合流检测（#259 P-3 E 段）：将发承诺 ∪ 已发/正在发断言（**强制**
    媒体语境）∪ 过去时假声明，任一命中返回 'image'/'voice'/''。

    用途：同会话 5 分钟内刚真发过一张、本轮又出一句配文体文本（「Just took this one
    for you—」）而本轮没有图——这句离开图就是谎，调用方据此「必须再附图，否则改写」。
    刻意绕开 ``wants_media`` 语境闸：那道闸在图真发出后就闭合了（请求已兑现），
    正是 14:49:51 第二句放行的原因。
    """
    return (detect_media_promise(text)
            or detect_media_claim(text, media_context=True)
            or detect_sent_claim(text))


def strip_photo_caption_claims(text: str) -> str:
    """与 :func:`detect_photo_caption_claim` 对称的句级剥离（承诺 + 断言 + 假声明）。"""
    out = strip_media_promises(text)
    out = strip_media_claims(out, media_context=True)
    return strip_sent_claims(out)


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


# ── 「想看的是什么」主体抽取（P1 图文一致性，2026-08-08）────────────────────
# 实录事故：AI 说「我煮了燕窝粥，要不要看看？」→ 客户「你有照片就发我看一下」→
# detect_selfie_request 命中泛化要图 → 相册里只有自拍 → 发出人脸照配文「家里
# 随便吃吃嘛」。根因是 offer 的**主体**在链路上全程丢失，只剩一个 'image'。

# 人像词：这些是「看看我」类提议，本就该走自拍链，不算非人像主体。
_PERSON_SUBJECT_WORDS = (
    "自拍", "照片", "相片", "图片", "圖片", "视频", "視頻", "影片", "视屏",
    "样子", "樣子", "长相", "長相", "素颜", "素顏", "近照", "美照", "脸", "臉",
    "本人", "自己",
)
# 抽出来但没信息量的壳词（宁可不判，也别拿它当"想看的东西"）
_VAGUE_SUBJECT_WORDS = (
    "一下", "一个", "一個", "这个", "這個", "那个", "那個", "什么", "什麼",
    "东西", "東西", "时候", "時候", "地方", "别的", "別的", "其他", "一点",
)
# 「要不要看/给你看」类展示提议标记——没有它就不是在提议展示，别乱抽名词。
_SHOW_MARKERS = (
    "要不要看", "想不想看", "想看", "给你看", "給你看", "给你瞧", "給你瞧",
    "给你瞅", "給你瞅", "拍给你", "拍給你", "发给你", "發給你", "看看吗",
    "看看嗎", "让你看", "讓你看", "给你晒", "給你曬",
    # 实施69：宾语前置承诺「给你拍一张X」也是展示提议——不认它，承诺句里的
    # 主体（大腰子）就无法经 offer-主体桥传给后续「照片呢」的兑现轮。
    "给你拍", "給你拍", "给你来一张", "給你來一張",
)
# 名词候选里不该出现的功能字（代词/动词/助词）——正则难免把「你有」「发张自」
# 这类片段当名词捞出来，字级黑名单比继续堆正则可靠。
_FUNCTION_CHARS = set(
    "你我他她它您咱有没沒就都也还還很是在和跟给給让讓把将將拍发發传傳送看想要"
    "能会會可以过過再又的了吗嗎呢吧啊呀哦嘛么麼这這那什怎"
)
# 量词开头不能算名词（「那碗粥」抽成「碗粥」是实测踩到的坑）
_QUANTIFIER_CHARS = "碗杯锅鍋盘盤份个個只隻张張条條瓶袋盆束件套"
_NOT_QUANT = f"(?![{_QUANTIFIER_CHARS}])"
# 「（我）刚+动作+了+名词」：AI 亲口说做了/买了个具体东西
_MADE_SUBJECT_RE = re.compile(
    r"(?:我|刚刚|剛剛|刚|剛|才|今天|昨天)(?:刚刚|剛剛|刚|剛|才|今天|昨天|自己|又)*"
    r"(?:煮|做|烤|炖|燉|熬|泡|蒸|炒|买|買|点|點|种|種|养|養|画|畫|织|織|"
    r"收拾|整理|布置|摆|擺|插)了?"
    r"(?:一|两|兩|几|幾|点|點|些)?"
    rf"[{_QUANTIFIER_CHARS}]?"
    rf"({_NOT_QUANT}[\u4e00-\u9fff]{{2,6}})"
)
# 「给你看看我的卧室」：直接点名要展示的东西/地方
_SHOW_SUBJECT_RE = re.compile(
    r"(?:给|給|让|讓)你(?:看看|瞧瞧|瞅瞅|看下|看一下|晒晒|曬曬)\s*"
    r"(?:我(?:的|家的|家里的|家裡的)?)?\s*"
    rf"({_NOT_QUANT}[\u4e00-\u9fff]{{2,6}})"
)
# 「给你拍一张刚烤好的大腰子」：宾语前置承诺里点名的主体（实施69）。
# 「的」锚定版优先（修饰语被「的」隔开，名词紧随其后）；直取版兜底
# （「给你拍一张大腰子」）——直取版贪婪吃进修饰语时会含「的」等功能字，
# 由 _clean_subject 整体否决，不会抽出脏主体。
_SHOW_BENEFACTIVE_DE_RE = re.compile(
    r"(?:给|給)\s*你\s*(?:拍|来|來)[^，。！？!?\n]{0,10}的\s*"
    rf"({_NOT_QUANT}[\u4e00-\u9fff]{{2,6}})"
)
_SHOW_BENEFACTIVE_RE = re.compile(
    r"(?:给|給)\s*你\s*拍(?!手|马|馬|肩|背|板|桌|脑|腦)\s*(?:一?[张張个個])?\s*"
    rf"({_NOT_QUANT}[\u4e00-\u9fff]{{2,6}})"
)
# 客户侧：「燕窝粥的照片」「那碗粥拍给我看看」「给我看看你卧室」
_REQ_OF_PHOTO_RE = re.compile(
    rf"({_NOT_QUANT}[\u4e00-\u9fff]{{2,6}})(?:的)?(?:照片|相片|图片|圖片)"
)
_REQ_SHOW_RE = re.compile(
    r"(?:把|将|將)?\s*(?:那|这|這)?"
    rf"[{_QUANTIFIER_CHARS}]?\s*"
    rf"({_NOT_QUANT}[\u4e00-\u9fff]{{2,6}})\s*"
    r"(?:拍|发|發|传|傳)(?:给|給)?我?(?:看看|看一下|看下|看)"
)
_REQ_SHOW_ME_RE = re.compile(
    r"(?:给|給|让|讓)我(?:看看|瞧瞧|瞅瞅|看下|看一下)\s*"
    r"(?:你(?:的|家的|家里的|家裡的)?)?\s*"
    rf"({_NOT_QUANT}[\u4e00-\u9fff]{{2,6}})"
)
# 「拍照烤串的给我看看」：动词前置的客户点名（实施69 实录漏网语序）。
_REQ_SHOOT_SHOW_RE = re.compile(
    rf"拍照?\s*({_NOT_QUANT}[\u4e00-\u9fff]{{2,6}})\s*(?:的)?\s*"
    r"(?:拍|发|發|传|傳)?(?:给|給|俾)\s*我\s*(?:看|睇)"
)


def _clean_subject(raw: Any) -> str:
    """名词候选清洗：削掉尾部虚词，剔除人像词/壳词/带功能字的片段。

    判不准一律返回 ""——抽错主体会让「无货不发图」误拦正常自拍请求，
    宁可退化成旧行为（照发自拍），也不能因为一次误抽把图卡掉。
    """
    s = str(raw or "").strip()
    while s and s[-1] in "的了吗嗎呢吧啊呀哦嘛么麼":
        s = s[:-1]
    # 贪婪匹配常把「发张」「你有」这类前缀吃进候选（finditer 不会回头试子串），
    # 先削前缀再判，比让整条候选作废更少漏。
    while len(s) > 2 and (s[0] in _FUNCTION_CHARS or s[0] in _QUANTIFIER_CHARS):
        s = s[1:]
    if len(s) < 2 or len(s) > 6:
        return ""
    if s[0] in _QUANTIFIER_CHARS:  # 「那碗粥」削掉「那」后剩「碗粥」＝量词开头，不是名词
        return ""
    if any(w in s for w in _PERSON_SUBJECT_WORDS):
        return ""
    if s in _VAGUE_SUBJECT_WORDS:
        return ""
    if any(c in _FUNCTION_CHARS for c in s):
        return ""
    return s


def detect_show_offer_subject(text: str) -> str:
    """出站 AI 文本里「提议展示的**非人像**主体」（如 燕窝粥/卧室）；没有返回 ""。

    与 :func:`detect_media_offer` 互补：那个只回答"有没有在提议发图"，这个回答
    "提议给人看的是什么"——后者才是图文一致性需要的信息。保守取值：必须同时出现
    展示提议标记与可识别名词，抽到人像词（自拍/样子）一律视作普通自拍 offer。
    """
    s = str(text or "")
    if not s.strip() or len(s) > 400:
        return ""
    if not any(m in s for m in _SHOW_MARKERS):
        return ""
    for rx in (_SHOW_SUBJECT_RE, _SHOW_BENEFACTIVE_DE_RE,
               _SHOW_BENEFACTIVE_RE, _MADE_SUBJECT_RE):
        for m in rx.finditer(s):
            sub = _clean_subject(m.group(1))
            if sub:
                return sub
    return ""


def _requested_subject(text: str) -> str:
    """客户本条里直接点名的非人像主体（「燕窝粥的照片」「给我看看你卧室」）。"""
    s = str(text or "")
    if not s.strip() or len(s) > 200:
        return ""
    for rx in (_REQ_OF_PHOTO_RE, _REQ_SHOW_ME_RE, _REQ_SHOOT_SHOW_RE,
               _REQ_SHOW_RE):
        for m in rx.finditer(s):
            sub = _clean_subject(m.group(1))
            if sub:
                return sub
    return ""


def wanted_media_subject(
    peer_text: str,
    history: Optional[Sequence[Dict[str, Any]]] = None,
    *,
    generic_request: bool = False,
    lookback: int = 6,
) -> str:
    """这次要发的图，对方想看的**非人像主体**；普通自拍请求返回 ""。

    两个来源：① 客户本条直接点名（「燕窝粥的照片」）；② 客户本条只是泛化要图
    （「你有照片就发我看看」/短肯定「好呀」）而最近一条 assistant 消息提议展示的
    是个具体东西——此时「发照片」指的是**那个东西**，不是自拍。
    ``generic_request`` 由调用方给（已判定这是一次要图），避免本模块反向依赖
    selfie 意图判定；未给时短肯定也算。``lookback``＝往回看几条消息找 offer。
    """
    own = _requested_subject(peer_text) or detect_show_offer_subject(peer_text)
    if own:
        return own
    if not (generic_request or is_short_affirmative(peer_text)):
        return ""
    seen = 0
    for m in reversed(list(history or [])):
        if seen >= max(1, int(lookback or 1)):
            break
        seen += 1
        if str((m or {}).get("role") or "") != "assistant":
            continue
        sub = detect_show_offer_subject(str((m or {}).get("content") or ""))
        if sub:
            return sub
        # 最近一条 assistant 若在提议发自拍（无具体主体）→ 就是普通自拍请求，别再往回翻
        if detect_media_offer(str((m or {}).get("content") or "")):
            return ""
    return ""


__all__ = [
    "KIND_IMAGE", "KIND_VIDEOCALL", "KIND_VOICE",
    "detect_media_promise", "strip_media_promises",
    "detect_media_claim", "strip_media_claims", "wants_media",
    "detect_sent_claim", "strip_sent_claims",
    "detect_photo_caption_claim", "strip_photo_caption_claims",
    "honest_no_photo_line", "lie_caught_honest_line", "lie_caught_resend_caption",
    "build_promise_rewrite_instruction", "build_photo_unsent_rewrite_instruction",
    "deflection_line",
    "detect_media_offer", "is_short_affirmative", "offer_accepted",
    "detect_show_offer_subject", "wanted_media_subject",
]
