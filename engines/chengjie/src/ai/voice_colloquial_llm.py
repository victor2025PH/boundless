"""LLM 口语化改写（活人感 A 档，2026-07-14）——本地小模型深度口语化，规则档兜底。

背景（语音活人感诊断的最大单点杠杆）：规则档 ``voice_colloquial.colloquialize`` 零延迟、
确定性，但只做「书面词替换 + 句首迟疑词」，一段话中间仍是书面结构。LLM 档用 LAN 本地
模型（``ai.fallback`` 的 qwen3:30b，**不占云成本**）做**语境贴合**的口语化——拆生硬长句、
自然口语连接、语气词落在该落的地方，更接近「真人在说」。

把「非确定性 LLM + 有延迟」驯服成生产可用的四道工程护栏：
  1. **缓存**（进程级 LRU，键=原文+情绪+lead+style）：非确定性结果缓存化 → 同原文永远
     同口语版（对 TTS 缓存/预渲染键友好）+ 省重复调用。
  2. **端点熔断**：连续失败 ≥N 次进冷却期，冷却期直接回落（不让挂掉的端点每条拖满超时）。
  3. **输出消毒 + 校验**：剥元话语前缀/引号；长度[0.3x,1.8x]、语言（中文）、非空校验
     不过 → 判为异常回落（防 LLM 发挥过度/截断/输出解释/串语言）。
  4. **失败即回落**：任何异常/超时/校验不过 → ``None``，调用方回落规则档（绝不阻塞语音）。

短句 / 非中文 no-op 与规则档同口径（保预渲染命中 + 防中文口语词 garble 外语）。
``async``（在 TTS 合成 async 上下文调用）；``ai_client`` 可注入（测试）。

可单测纯函数：build_colloquial_prompt / sanitize_llm_output / _cache_key。
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 提示词版本：进缓存键（内存+落盘）——改 few-shot/规则后旧缓存自动失效，
# 防「提示词升级了、老句子还在念旧改写」。
# v3（2026-08-03 P0-1）：禁感叹词开场（「嘿病」源头之一）+ 句长上限（治长句念稿）。
# v4（2026-08-30 #92）：事实红线补「自述行为/饮食」——晚餐自述被改写链改飞的实锤
# （原文说 A 顿饭、口语版念出 B 顿饭，语义余弦拦不住同域换菜）。
_PROMPT_VERSION = 4

# ── 情绪 → 语气词（喂给 LLM 的语气基调）─────────────────────────────────────
_EMOTION_TONE = {
    "neutral": "自然放松", "warm": "温暖亲切", "happy": "开心愉快",
    "playful": "俏皮活泼", "excited": "兴奋雀跃", "empathetic": "温柔共情",
    "apologetic": "诚恳", "calm": "平静舒缓", "sad": "低落轻声", "serious": "认真",
}


# ── 口语重塑力度（AI Live OS Agent6，2026-07-24）───────────────────────────────
# rewrite_intensity 三档，力度渐进但**事实红线三条恒定**（不改值/不编造/不串语言）：
#   light  = 仅换词 + 拆句（最保守，即使 lead=True 也不加句首连接；事实零风险）
#   natural= + 句首口语连接 + 语气词（当前生产默认，≈ 旧行为）
#   vivid  = + 主观口吻框架（「说真的/我跟你讲」）+ 停顿感（主播味/陪伴感最强）
_INTENSITY = ("light", "natural", "vivid")

# 「转述而非回答」硬规则 + few-shot（2026-08-01，生产实锤失败模式的针对性矫正）：
# 7-30B 本地模型最常见的两类翻车——① 把「待改写的原文」当成「对方发来的消息」去
# **回答/接话**（原文问「你吃饭了吗」→ 输出「还没呢你呢」）；② 篡改自称/名字
# （「我叫林佳欣」→「我叫小六」）。两条守卫（锚点/语义余弦）只能事后拒，拒了就
# 掉规则档＝书面稿照念。few-shot 从源头教会小模型「同一句话换成口语说出来」。
_ANTI_ANSWER_RULE = (
    "你不是在和任何人对话：给你的文字是「你自己接下来要说的话」，"
    "你的任务只是把它改成口语说法。绝不能回答它、接它的话——"
    "原文在提问，改写后必须还是**同一个提问**；"
    "原文里的自称、名字、身份必须原样保留。"
)
_FEWSHOT_BLOCK = (
    "示例（体会「同一句话的口语说法」——不是回答）：\n"
    "原文：因此我们无需担心这个问题，系统会自动进行处理。\n"
    "口语版：所以这个咱不用担心哈，系统自己会处理的。\n"
    "原文：我叫林佳欣，你呢，怎么称呼比较好？\n"
    "口语版：我叫林佳欣呀，你呢，我叫你什么好呀？\n"
    "原文：我刚才在健身房锻炼完之后顺便去了趟超市买了点水果和牛奶，你今天过得怎么样？\n"
    "口语版：我刚练完，顺路去超市拎了点水果牛奶。你今天咋样呀？"
)


def build_colloquial_prompt(emotion: str = "neutral", lead: bool = True,
                            style: str = "", disfluency: bool = False,
                            intensity: str = "natural") -> str:
    """构建口语化系统提示（纯函数）。

    ``intensity``＝口语重塑力度 light|natural|vivid（AI Live OS Agent6，2026-07-24）：
    档位越高允许的主观口吻/停顿越多，但**事实红线恒定**（数字/时间/人名/金额/承诺不改、
    不编造、不串语言）。``lead``＝是否允许句首口语连接（分条非首条关）；
    ``disfluency``＝允许一处轻口误自纠（⑥，调用方按文本 crc 低频开启）。
    """
    lvl = str(intensity or "natural").strip().lower()
    if lvl not in _INTENSITY:
        lvl = "natural"
    tone = _EMOTION_TONE.get(str(emotion or "").strip().lower(), "自然放松")
    if lvl == "vivid":
        # 陪伴态口语重塑：像真人随口说，而不是念稿——红线不变，放开主观口吻/停顿。
        parts = [
            "你是「把 AI 写的回复变成真人会怎么说」的口语重塑助手。"
            "目标：听起来像一个真实的人在微信语音里随口说，而不是念稿。",
            "红线（违反即失败）：",
            "1. 事实不可变——数字、时间、日期、金额、人名、地名、任何承诺，值和含义一律原样保留；",
            "2. 不编造——不得加入原文没有的具体事实（可加语气/口头禅/口语连接，但不加「信息」）；"
            "原文里「我吃了什么/我在做什么」这类自述是事实：吃的什么饭、做的什么事"
            "一个都不能换、不能加（原文没提的菜名饭名绝不能出现）；",
            "3. 同一种语言，不串语言。",
            "重塑方式：",
            "- 拆开生硬长句，用日常口语词（因此→所以、是否→是不是、非常→特别、无需→不用）；",
            "- 一句超过十五个字就拆成两句，宁可多用句号——短句才像说话，长句像念稿；",
            "- 陪伴对话把「您」改成「你」（「您好」除外），去掉客服腔/播音腔；",
            "- 允许加一点点主观口吻框架（「说真的」「我跟你讲」放在句中），但别每句都加；",
            f"- 语气{tone}；",
            "- 可以有轻微语气词和自然停顿感（用省略号……表示换气），像真的在想着说；",
            "- 开头第一个字直接进入正文：不要用「嘿/哈哈/哎呀/嗨/诶/哎哟」这类感叹词、"
            "笑声或口头禅开场——连续几条都同款开场一听就是机器；笑意靠语气不靠字面；",
            "- 真人微特征（每条最多选一种，别堆；没有把握就不用）：",
            "  · 思考时轻轻重复一个词（「我觉得……我觉得可以」）；",
        ]
        if str(style or "").strip():
            parts.append(f"- 说话风格：{str(style).strip()}；")
        if disfluency:
            parts.append(
                "- 本条允许一处很轻的自然口误自纠（如「明天…啊不对，后天」），"
                "优先于上面的微特征，随意不刻意；")
        parts.append(_ANTI_ANSWER_RULE)
        parts.append(_FEWSHOT_BLOCK)
        parts.append("只输出重塑后的那段话本身，不要引号、不要解释、不要任何前后缀。")
        return "\n".join(parts)
    # light / natural：保守档（事实零风险，力度渐进）
    parts = [
        "你是口语改写助手。把用户给的一句话改写成【适合用语音说出来】的口语版本。",
        "严格要求：",
        "1. 保持原意和所有信息不变，绝对不能增加新信息、遗漏或篡改信息——"
        "数字、时间、日期、金额、人名、地名等关键信息必须原样保留（可用中文数字念法，但值不能变）；"
        "原文里「我吃了什么/我在做什么」这类自述同属关键信息："
        "吃的什么饭、做的什么事不能换，原文没提的菜名饭名绝不能出现；",
        "2. 把书面表达换成日常口语（如「因此」→「所以」、「是否」→「是不是」），拆开生硬的长句；",
        f"3. 语气{tone}；",
    ]
    if lvl == "natural" and lead:
        parts.append("4. 可以在开头加一点点自然的口语连接（如「其实」「话说」），但别每句都加；")
    else:
        parts.append("4. 直接说正文，不要用语气词或开场白开头；")
    if str(style or "").strip():
        parts.append(f"5. 说话风格：{str(style).strip()}；")
    if disfluency:
        parts.append("6. 可以带至多一处很轻的自然口误自纠（如「明天…啊不对，后天」），要随意不刻意；")
    parts.append(_ANTI_ANSWER_RULE)
    parts.append(_FEWSHOT_BLOCK)
    parts.append("只输出改写后的那一句话本身，不要加引号、不要解释、不要任何前后缀。")
    return "\n".join(parts)


# ── 语音剧本 Speech Script v1（实施65，2026-08-23）──────────────────────────
# 把「停哪/想哪/错哪」的决策权从 crc32/几何切分移交给懂语义的 LLM：口语化改写与
# 韵律标记合并为**一次** LLM 调用，输出单行剧本「段‖档 段‖档 段」。执行器在
# voice_pacing.parse_speech_script / paced_synthesize（自动识别）。任何一步失败
# → None → 调用方回落现行链，绝不劣化。
# 剧本提示词独立版本号（进缓存键）：改 build_speech_script_prompt 必须 bump——
# 刻意不用全局 _PROMPT_VERSION（bump 它会把普通口语化的全量落盘缓存一起作废）。
# v4（2026-08-23 老板耳测四轮后定向）：禁笑声标记（引擎念出来怪异），笑意用话带；
# 语调起伏交给分段+标点；文本口语度再加码（微信语音口吻）。
# v5（2026-08-30 #92）：红线补自述行为/饮食（与普通改写 v4 同批）。
_SCRIPT_PROMPT_VERSION = 5


def build_speech_script_prompt(emotion: str = "neutral", style: str = "",
                               disfluency: bool = False) -> str:
    """语音剧本系统提示（纯函数）：vivid 口语重塑 + 语义化停顿/呼吸/思考/口误标记。

    与 build_colloquial_prompt(vivid) 的关系＝超集：同一套事实红线，多出剧本标记
    协议。停顿标记只许出现在**意群边界**（格式上即段尾），这是实施65 的核心不变量。
    """
    tone = _EMOTION_TONE.get(str(emotion or "").strip().lower(), "自然放松")
    parts = [
        "你是「语音剧本」助手：把 AI 写的回复改成真人发微信语音时随口说的话，"
        "并用标记标注怎么说。输出**一行**剧本，不要换行。",
        "剧本格式：把话切成 2-8 个语段，每段一个完整的意思（5-25 字）；"
        "段与段之间用停顿标记隔开：‖短（顺着说）、‖中（正常换句）、‖长（说完一件事/换话题）。"
        "最后一段后面**不加**任何标记。",
        "红线（违反即废）：",
        "1. 数字、时间、日期、金额、人名、地名、承诺，值和含义一律原样保留；",
        "2. 不得加入原文没有的事实（语气词/口头禅可以加，「信息」不能加）——"
        "「我吃了什么/我在做什么」这类自述是事实：吃的什么饭、做的什么事"
        "不能换，原文没提的菜名饭名绝不能出现；",
        "3. 同一种语言，不串语言；",
        "4. 只输出剧本一行本身：不要引号、不要解释、不要复述规则。",
        "改写规则（先改写再分段，往「跟熟人发语音」的口吻改）：",
        "- 把书面词全换成嘴上说的词：非常→特别、但是→不过、如果→要是、"
        "现在→这会儿、之后→回头、一起→一块儿、可能→说不定、立刻→这就、"
        "研究→琢磨/捣鼓、发现→一看、包括→像什么；",
        "- 句子说短：一口气最多十二三个字，长句拆成几口气说；",
        "- 语气助词（呀/呢/啦/嘛/哟/啊）自然地撒几个，别每句都有；",
        "- 去客服腔播音腔，「您」改「你」；书面成语换大白话"
        "（丰盛的晚餐→整顿好吃的、令人惊讶→没想到）；",
        "- 如果原文已经很口语，就只分段打标记、少改字。",
        "停顿标记规则（最重要）：",
        "- 标记只能打在**一个意思说完的地方**，绝不能把一个意思劈成两半；",
        "- 顺着往下说、列举中间用‖短；一句话说完用‖中；"
        "说完一件事、要换话题、要开始问对方之前用‖长；",
        "- 结尾若是问对方的问题，它自己就是最后一段（后面没有标记）。",
        "笑的表达（重要）：**禁止使用任何笑声标记**（[laughter] 之类一律不写）；"
        "笑意用「说出来的话」带：比如「给我乐坏了」「差点笑岔气」「你说逗不逗」；"
        "「哈哈」整条最多出现一次、最多两个字（哈哈哈哈一长串念出来是假笑）。",
        "语调起伏（治「念稿式平调」）：",
        "- 讲事情的段落用平常陈述；讲到转折或爆点的那一段，末尾用感叹号"
        "（念的人会自然扬起来）；问对方的段落用轻快的问句；",
        "- 相邻两段别用一样的句式开头（连续「然后…然后…」是机器人）；",
        "- 关键的转折/爆点前用‖长（停一拍再说，比喊出来更像真人）。",
        "其他标记：",
        "- [breath] 只能放在某段的最开头＝先吸口气再说这段：整条 ≥40 字时"
        "**通常应有恰好 1 处**（放在最长/信息量最大的那一段开头），短回复不用；"
        "最多 2 处，带 [breath] 的段要 ≥12 字；",
        "- 思考词（嗯……/我想想）整条最多 1 处，只许出现在三种地方："
        "回答对方问题的段首、报数字或做选择之前、换话题的段首；",
        "- 不要新加「哎呀/嘿/哈哈」这类感叹词开场（原文本来就有的可以保留）。",
        f"语气{tone}。",
    ]
    if str(style or "").strip():
        parts.append(f"说话风格：{str(style).strip()}。")
    if disfluency:
        parts.append(
            "本条允许至多 1 处轻口误（只许在前三分之二的段落里，二选一）："
            "①重复式＝段首第一个词说两遍（「我、我跟你说」）；"
            "②自纠式＝把一个普通词说错后马上纠正（「上周…啊不对，上上周」），"
            "但数字/人名/金额/时间/地名**绝不能**当错词。没有自然的位置就不用。")
    parts.append(_ANTI_ANSWER_RULE)
    parts.append(
        "示例输入1：今天店里客人很多，卖得很好。你吃饭了吗？记得休息。\n"
        "示例输出1：哎今天店里人可多了，卖得特别好！‖长 嗯……你吃饭了没呀？‖短 记得歇会儿啊\n"
        "示例输入2：我今天下午去了一趟超市，买了很多东西，包括牛奶、鸡蛋和水果。"
        "晚上我打算做一顿丰盛的晚餐，犒劳一下自己。今天有个顾客的行为让我觉得很好笑。\n"
        "示例输出2：[breath]我下午去了趟超市，买了好些东西‖短 牛奶啊鸡蛋啊，"
        "还有点水果‖中 晚上寻思给自己整顿好吃的‖长 对了今天有个顾客，给我乐坏了‖短 回头说给你听哈")
    return "\n".join(parts)


_SCRIPT_MARK_RE = re.compile(r"‖\s*(短|中|长)")
_SCRIPT_FIX_RE = re.compile(r"啊不对|哦不对|说错了")
_SCRIPT_ANCHOR_CHAR_RE = re.compile(r"[0-9A-Za-z]")


def strip_speech_script(text: str) -> str:
    """剥掉剧本标记，还原纯文本（供语义比对/锚点校验/非剧本路径兜底）。"""
    t = _SCRIPT_MARK_RE.sub("", str(text or ""))
    t = t.replace("[breath]", "")
    return re.sub(r"\s+", " ", t).strip()


def sanitize_speech_script(raw: str, original: str, *,
                           max_expand: float = 2.6) -> Optional[str]:
    """剧本消毒：格式/预算/口误选址不合规 或 剥标记后过不了既有消毒 → None。

    复用 ``sanitize_llm_output`` 的全部事实守卫（长度/语言/锚点）——剧本只是
    「带标记的口语化产物」，事实红线与普通改写完全同一把尺。
    """
    t = str(raw or "").strip()
    if not t:
        return None
    t = re.sub(r"\s*\n+\s*", " ", t)
    t = _PREFIX_RE.sub("", t).strip().strip(_QUOTE_CHARS).strip()
    # v4 政策：笑声标记一律不进合成（引擎念出来怪异，老板耳测两轮定案）——
    # 模型违规写了就静默剥除，不废整条剧本；笑意由文字（「乐坏了」）表达。
    t = t.replace("[laughter]", "")
    if "‖" not in t:
        return None
    from src.ai.voice_pacing import parse_speech_script
    chunks = parse_speech_script(t)
    if not chunks:
        return None
    if t.count("[breath]") > 2:
        return None
    if sum(1 for c in chunks if c.get("think")) > 2:
        return None
    # 口误守卫：不许贴着事实锚点（±8 字符内有数字/拉丁即拒），不许出现在最后一段
    for m in _SCRIPT_FIX_RE.finditer(t):
        lo, hi = max(0, m.start() - 8), min(len(t), m.end() + 8)
        if _SCRIPT_ANCHOR_CHAR_RE.search(t[lo:hi]):
            return None
    if _SCRIPT_FIX_RE.search(str(chunks[-1].get("text") or "")):
        return None
    plain = strip_speech_script(t)
    if sanitize_llm_output(plain, original, max_expand=max_expand) is None:
        return None
    return t


# ── 输出消毒 / 校验（纯函数）─────────────────────────────────────────────────
_PREFIX_RE = re.compile(
    r"^\s*(口语版|口语|改写后?|结果|输出|回答|答案|译文)\s*[:：]\s*")
# 元话语标记：LLM 若加了解释段，从这些标记处截断（只保留改写正文）
_META_SPLIT_RE = re.compile(r"(?:\n\s*\n|解释[:：]|说明[:：]|注[:：]|原文[:：]|---)")
_QUOTE_CHARS = "「」『』“”\"'‘’"


_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
# 事实锚点：≥2 位数字 与 ≥2 字母的拉丁 token（金额/型号/品牌/套餐名）。
# 单位数/单字母噪声大（「一」「3点」的 3 常被合法改写成汉字）故不入锚点集。
_ANCHOR_RE = re.compile(r"\d[\d,.]*\d|[A-Za-z]{2,}")


def _anchor_set(text: str) -> set:
    """文本里的事实锚点（数字串归一去掉千分位/尾点，拉丁统一小写）。"""
    out = set()
    for tok in _ANCHOR_RE.findall(str(text or "")):
        t = tok.strip().lower().rstrip(".")
        if t and t[0].isdigit():
            t = t.replace(",", "").replace("，", "")
        if len(t) >= 2:
            out.add(t)
    return out


def lost_anchors(original: str, rewritten: str) -> list:
    """原文有、改写里没了的事实锚点（排序返回；空=红线未破）。

    口语化的书面契约里第一条红线就是「数字/金额/型号原样保留」，但此前**从未校验**
    ——2026-07-28 实录：本地模型把「团队版198美金」改写成「168美金」这类篡改，
    只要长度落在 0.6~1.8 倍就一路放行。锚点是确定性的、零误伤的那一半。
    """
    o = _anchor_set(original)
    if not o:
        return []
    return sorted(o - _anchor_set(rewritten))


# ── 餐食自述守卫（#92，0830 实锤「晚餐自述矛盾」）───────────────────────────
# 事故（钧/会话 Jenny，原图 812）：语音答「刚吃完晚饭，你泡面配点青菜鸡蛋饭没？
# 我这人懒，晚上就随便炒个饭对付」——句内餐食叙事混乱。改写链的三道守卫全拦
# 不住这类漂移：数字锚点只护数字/拉丁（CJK 菜名零保护）、语义余弦对「同域换菜」
# 天然高分（晚饭话题→晚饭话题 cos≈0.9+）、长度守卫无关。确定性收口＝**改写
# 不得引入原文没有的餐食名词**（原文没提的菜名饭名出现在口语版里=编造）；
# 拒绝的代价仅是回落规则档（原文照念），零损失，故词表可以偏宽。
# 刻意只查「新增」不查「丢失」：口语化省略修饰（「青菜鸡蛋面」→「面」）是
# 合法压缩，反向查会大面积误拒。
_FOOD_TERMS = (
    "早饭", "早餐", "午饭", "午餐", "晚饭", "晚餐", "夜宵", "宵夜",
    "泡面", "方便面", "螺蛳粉", "炒饭", "蛋炒饭", "炒面", "拌面", "汤面",
    "米饭", "面条", "饺子", "包子", "馒头", "馄饨", "米线", "米粉",
    "火锅", "烧烤", "串串", "外卖", "披萨", "汉堡", "寿司", "拉面",
    "青菜", "鸡蛋", "牛肉", "猪脚", "鸡腿", "排骨", "烤肉", "麻辣烫",
    "沙拉", "蛋糕", "奶茶", "咖啡", "豆浆", "稀饭",
)
_FOOD_SUFFIX_RE = re.compile(
    r"[\u4e00-\u9fff]{1,4}(?:炒饭|盖饭|拌饭|烩饭|焖饭|蛋饭|炒面|拌面|汤面)")


def food_terms(text: str) -> set:
    """文本中的餐食名词集合（词表 + 「X饭/X面」复合后缀）。纯函数。"""
    s = str(text or "")
    out = {t for t in _FOOD_TERMS if t in s}
    for m in _FOOD_SUFFIX_RE.finditer(s):
        out.add(m.group(0))
    return out


def introduced_food_terms(original: str, rewritten: str) -> list:
    """改写**新增**的餐食名词（原文没提的菜名饭名；空=餐食红线未破）。

    复合词拆解防误报：原文「青菜鸡蛋面」被拆说成「青菜」「鸡蛋」不算新增
    ——新增 term 若是原文任一餐食词/原文文本的子串则豁免。
    """
    o_text = str(original or "")
    intro = []
    for t in sorted(food_terms(rewritten) - food_terms(o_text)):
        if t in o_text:
            continue          # 原文正文本就含该词（如复合词内嵌）
        intro.append(t)
    return intro


# ── 疑问句主导检测（2026-08-10 实录矫正）────────────────────────────────────
# 事故：坐席手打「你晚上吃饭了吗，晚上有什么安排」被本地模型改写成**对它的回答**
# （「你呢吃了吗，晚上打算随便弄点东西吃，然后在家呆着」）念出——答话与原问同
# 话题，嵌入余弦天然偏高，语义守卫拦不稳。问句本就是口语高频形态，改写收益≈0，
# 而「把问句答掉」恰是小模型最高发的翻车面 → 问句主导的文本直接跳过 LLM 档
# （规则档照常＝原文近乎照念，永不劣化）。
# 粒度＝**子句级**（逗号/顿号也拆）：「陈述，结尾问句？」是自动链最常见的正常
# 改写对象（few-shot 范例即此形态），只有**每个实义子句都像疑问**才判主导——
# 全问句时模型没有可改写的陈述内容，只剩「回答它」一条歧路。
_CLAUSE_RE = re.compile(r"[^。！!？?…\n，,；;、]+[。！!？?…\n，,；;、]*")
_QUESTION_TAIL_RE = re.compile(r"(吗|嘛|呢|么)\s*[～~。.…!！]*$")
_QUESTION_WORD_RE = re.compile(
    r"什么|啥|怎么|怎样|咋样|咋办|多少|几点|几个|哪里|哪儿|哪个|为什么|干嘛|谁|吗|呢")


def is_interrogative_dominant(text: str) -> bool:
    """文本是否「问句主导」（每个实义子句都是疑问形态）。纯函数。

    子句疑问判据（任一命中）：带问号收尾 / 疑问语气词收尾（吗/呢/嘛/么）/
    含疑问代词（什么/怎么/谁…）。≤2 字的语气短句（诶/哈哈）不投票；
    出现任何实义陈述子句 → False（混合文本改写价值在陈述段，漂移风险由
    语义守卫兜底）。
    """
    core = str(text or "").strip()
    if not core:
        return False
    saw_question = False
    for m in _CLAUSE_RE.finditer(core):
        seg = m.group(0).strip()
        if not seg:
            continue
        body = seg.rstrip("。！!？?…～~ \t\n，,；;、")
        tail = seg[len(body):]
        if not body:
            continue
        if "？" in tail or "?" in tail:
            saw_question = True
            continue
        if _QUESTION_TAIL_RE.search(body) or _QUESTION_WORD_RE.search(body):
            saw_question = True
            continue
        if len(body) <= 2:
            continue                      # 语气词/短叹不投票（诶/嗯嗯/哈哈）
        return False
    return saw_question


def sanitize_llm_output(
    raw: str, original: str, *, max_expand: float = 1.8, min_keep: float = 0.6,
) -> Optional[str]:
    """消毒 + 校验 LLM 口语化输出。异常（空/超长/超短/串语言/元话语/丢锚点）→ None。纯函数。"""
    from src.ai.voice_colloquial import _is_chinese_dominant

    t = str(raw or "").strip()
    if not t:
        return None
    t = _PREFIX_RE.sub("", t).strip()
    # 元话语段截断（防「改写正文\n\n解释：...」把解释念出来）
    m = _META_SPLIT_RE.search(t)
    if m:
        t = t[:m.start()].strip()
    t = t.strip(_QUOTE_CHARS).strip()
    if not t:
        return None
    core = str(original or "").strip()
    if not core:
        return None
    # 长度守卫：过短=截断/丢信息，过长=发挥过度/夹带解释（+8 给短文本余量）。
    # 缩水下限 0.3→0.6（2026-07-27 实锤：讲故事回复被口语化 67→33 腰斩，客户听到
    # 半截故事投诉「怎么说话说一半」——口语化是**改写**不是摘要，砍掉 >40% 必是
    # 内容丢失；拒了回落规则档＝完整原文照念，永远不劣化）。
    if len(t) < len(core) * float(min_keep):
        return None
    if len(t) > len(core) * float(max_expand) + 8:
        return None
    # 语言守卫：原文中文而输出串了别的语言 → 拒（防 garble）
    if not _is_chinese_dominant(t):
        return None
    # 事实锚点守卫：数字/金额/型号丢了或被改 → 拒（红线，零误伤的那一半；
    # 语义漂移那一半在 llm_colloquialize 里用嵌入余弦兜，见该函数注释）
    _lost = lost_anchors(core, t)
    if _lost:
        logger.info("[voice_colloquial_llm] 拒改写：事实锚点丢失 %s", _lost[:3])
        return None
    # #92 餐食自述守卫：改写引入原文没有的菜名饭名 → 拒（同域换菜余弦拦不住，
    # 确定性词表收口；回落=原文照念零损失）
    _intro = introduced_food_terms(core, t)
    if _intro:
        logger.info("[voice_colloquial_llm] 拒改写：引入原文没有的餐食词 %s（#92）",
                    _intro[:3])
        return None
    return t


# ── 缓存（进程级 LRU）────────────────────────────────────────────────────────
_CACHE: "OrderedDict[str, str]" = OrderedDict()
_CACHE_MAX = 512
_CACHE_LOCK = threading.Lock()


def _cache_key(text: str, emotion: str, lead: bool, style: str,
               disfluency: bool = False, intensity: str = "natural",
               temperature: float = 0.5) -> str:
    raw = (f"v{_PROMPT_VERSION}\x1f{text}\x1f{emotion}\x1f{int(bool(lead))}\x1f{style}"
           f"\x1f{int(bool(disfluency))}\x1f{intensity}\x1f{round(float(temperature), 2)}")
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _cache_get(key: str) -> Optional[str]:
    with _CACHE_LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
    return None


def _cache_put(key: str, value: str) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = value
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)


# ── 落盘缓存（2026-08-01）────────────────────────────────────────────────────
# 生产实例重启频繁（当日实测 ~1h 一次），进程级 LRU 每次清零 → 高频短句反复烧
# LLM。改写是内容寻址的（键含原文+情绪+风格+提示词版本），落 SQLite 跨重启复用。
# 只持久化**成功改写**——「改不出/被拒」可能是端点抖动或采样运气，落盘会把一次
# 临时故障固化成「这句永远不再尝试」。路径经 licensing.data_paths.config_dir()
# （生产=实例数据根 config/，测试被 conftest 的 AITR_DATA_DIR 指到 tmp，绝不写仓库）。
_DISK_FILE = "voice_colloquial_cache.db"
_DISK_CAP = 4000
_DISK_LOCK = threading.Lock()
_disk_hits = 0


def _disk_path() -> Optional[str]:
    try:
        from src.licensing.data_paths import config_dir
        d = config_dir()
        d.mkdir(parents=True, exist_ok=True)
        return str(d / _DISK_FILE)
    except Exception:
        return None


def _disk_get(key: str) -> Optional[str]:
    global _disk_hits
    path = _disk_path()
    if not path:
        return None
    try:
        import sqlite3
        with _DISK_LOCK:
            # 注意显式 close：sqlite3 的 with 只管事务不关连接——句柄泄漏会让
            # Windows 上的 reset_state()/轮转 unlink 静默失败（缓存跨进程幽灵存活）。
            conn = sqlite3.connect(path, timeout=2.0)
            try:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS rewrites("
                    "key TEXT PRIMARY KEY, value TEXT NOT NULL, ts REAL NOT NULL)")
                row = conn.execute(
                    "SELECT value FROM rewrites WHERE key=?", (key,)).fetchone()
                conn.commit()
            finally:
                conn.close()
        if row and row[0]:
            _disk_hits += 1
            return str(row[0])
    except Exception:
        logger.debug("[voice_colloquial_llm] 落盘缓存读失败（忽略）", exc_info=True)
    return None


def _disk_put(key: str, value: str) -> None:
    if not value:
        return                      # 只存成功改写，见模块注释
    path = _disk_path()
    if not path:
        return
    try:
        import sqlite3
        with _DISK_LOCK:
            conn = sqlite3.connect(path, timeout=2.0)
            try:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS rewrites("
                    "key TEXT PRIMARY KEY, value TEXT NOT NULL, ts REAL NOT NULL)")
                conn.execute(
                    "INSERT OR REPLACE INTO rewrites(key, value, ts) VALUES(?,?,?)",
                    (key, value, time.time()))
                n = conn.execute("SELECT COUNT(*) FROM rewrites").fetchone()[0]
                if n > _DISK_CAP:
                    conn.execute(
                        "DELETE FROM rewrites WHERE key IN ("
                        "SELECT key FROM rewrites ORDER BY ts ASC LIMIT ?)",
                        (max(1, n - _DISK_CAP),))
                conn.commit()
            finally:
                conn.close()
    except Exception:
        logger.debug("[voice_colloquial_llm] 落盘缓存写失败（忽略）", exc_info=True)


# ── LAN 端点直连（2026-08-01，「176 的逻辑」落地为可迁移端点列表）──────────────
# ``colloquial.llm_endpoints``：口语化专属 LAN 端点列表（与 ai.fallback 解耦——
# 兜底 LLM 要 30B 出对话质量，改写只要 7-9B 快模型；两者落点可以不同机器）。
# 逐端点失败冷却（默认 90s）：挂掉的端点不再每条语音拖满超时，自动走下一个；
# 全列表失败才轮到 provider 顺位的下一档（cloud）。缺省（不配）＝沿用
# ``ai.fallback``（client.rewrite_local），老部署行为不变。
_EP_COOLDOWN_SEC = 90.0
_EP_BAD_UNTIL: Dict[str, float] = {}
_EP_LOCK = threading.Lock()
_PROVIDER_STATS: Dict[str, Dict[str, int]] = {}
_last_provider = ""


def parse_llm_endpoints(raw: Any) -> Tuple[Dict[str, Any], ...]:
    """解析 ``colloquial.llm_endpoints`` 配置 → 端点元组（坏条目静默剔除）。纯函数。"""
    out: List[Dict[str, Any]] = []
    for item in (raw if isinstance(raw, (list, tuple)) else []):
        if not isinstance(item, dict):
            continue
        base = str(item.get("base_url") or "").strip().rstrip("/")
        model = str(item.get("model") or "").strip()
        if not base or "://" not in base or not model:
            continue
        try:
            timeout = float(item.get("timeout_sec", 6.0) or 6.0)
        except (TypeError, ValueError):
            timeout = 6.0
        try:
            num_ctx = int(item.get("num_ctx", 8192) or 8192)
        except (TypeError, ValueError):
            num_ctx = 8192
        out.append({
            "base_url": base,
            "model": model,
            "api_key": str(item.get("api_key") or "ollama").strip() or "ollama",
            "timeout_sec": max(1.0, timeout),
            "num_ctx": max(0, num_ctx),
            "keep_alive": str(item.get("keep_alive") or "").strip(),
        })
    return tuple(out)


def _endpoint_key(ep: Dict[str, Any]) -> str:
    return f"{ep['base_url']}|{ep['model']}"


def _ep_in_cooldown(key: str) -> bool:
    with _EP_LOCK:
        return time.monotonic() < _EP_BAD_UNTIL.get(key, 0.0)


def _note_provider(key: str, ok: bool) -> None:
    global _last_provider
    with _EP_LOCK:
        st = _PROVIDER_STATS.setdefault(key, {"ok": 0, "fail": 0})
        st["ok" if ok else "fail"] += 1
        if ok:
            _last_provider = key
            _EP_BAD_UNTIL.pop(key, None)
        elif key != "cloud" and key != "fallback":
            _EP_BAD_UNTIL[key] = time.monotonic() + _EP_COOLDOWN_SEC


def get_last_provider() -> str:
    """最近一次成功改写用的 provider 键（观测用，best-effort）。"""
    with _EP_LOCK:
        return _last_provider


def _record_rewrite_cost(ep: Dict[str, Any], usage: Dict[str, Any],
                         t0: float, *, openai_shape: bool) -> None:
    """口语化改写记入 llm_cost（tier=``colloquial``），best-effort 绝不抛。

    2026-08-13 内测读数盲区补全：这条链 httpx 直连不经 ai_client，出话分布
    看板此前完全看不见它——「vLLM 收到 N 次请求 vs 看板只记 1 次」的主要缺口。
    """
    try:
        from src.ai.llm_cost import get_llm_cost
        if openai_shape:
            pt = int((usage or {}).get("prompt_tokens") or 0)
            ct = int((usage or {}).get("completion_tokens") or 0)
        else:  # Ollama 原生 /api/chat 顶层字段
            pt = int((usage or {}).get("prompt_eval_count") or 0)
            ct = int((usage or {}).get("eval_count") or 0)
        get_llm_cost().record(
            model=str(ep.get("model") or "colloquial"),
            prompt_tokens=pt, completion_tokens=ct,
            tier="colloquial",
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
    except Exception:
        pass


async def _rewrite_via_endpoint(
    ep: Dict[str, Any], system: str, user: str, *,
    temperature: float, max_tokens: int = 240,
) -> Optional[str]:
    """直连一个 LAN 端点做单轮改写。Ollama 端点走原生 /api/chat
    （/v1 兼容层不认 keep_alive/think——与 ai_client._fb_native_chat 同教训）；
    base_url 以 /v1 结尾则按 OpenAI 兼容口调。异常直抛，调用方记冷却。"""
    import httpx

    messages = [
        {"role": "system", "content": str(system or "")},
        {"role": "user", "content": str(user or "")},
    ]
    base = ep["base_url"]
    to = httpx.Timeout(ep["timeout_sec"], connect=3.0)
    t0 = time.monotonic()
    if base.endswith("/v1"):
        headers = {"Authorization": f"Bearer {ep['api_key']}"}
        payload: Dict[str, Any] = {
            "model": ep["model"], "messages": messages,
            "temperature": temperature, "max_tokens": max_tokens,
            # vLLM(Qwen3 系)直答档：改写链打 173:8001(chatx=Qwen3.6-27B) 时思考档会吃满
            # timeout_sec 预算；enable_thinking=False 保 0.3-0.9s 快改写。Ollama /v1 忽略。
            "chat_template_kwargs": {"enable_thinking": False},
        }
        async with httpx.AsyncClient(timeout=to) as hc:
            resp = await hc.post(
                base + "/chat/completions", json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        msg = ((data.get("choices") or [{}])[0].get("message") or {})
        out = (msg.get("content") or "").strip() or None
        if out:
            _record_rewrite_cost(ep, data.get("usage") or {}, t0, openai_shape=True)
        return out
    payload = {
        "model": ep["model"], "messages": messages,
        "stream": False, "think": False,
        "options": {"num_predict": max_tokens, "temperature": temperature},
    }
    if ep["num_ctx"] > 0:
        payload["options"]["num_ctx"] = ep["num_ctx"]
    if ep["keep_alive"]:
        payload["keep_alive"] = ep["keep_alive"]
    async with httpx.AsyncClient(timeout=to) as hc:
        resp = await hc.post(base + "/api/chat", json=payload)
        resp.raise_for_status()
        data = resp.json()
    out = ((data.get("message") or {}).get("content") or "").strip() or None
    if out:
        _record_rewrite_cost(ep, data, t0, openai_shape=False)
    return out


# ── 守卫计数器（P2 2026-08-11：「改写了多少、拒了多少、为什么拒」看板化）──────
# 进程级 best-effort 观测；随 health_signal() 出 avatar-status / metrics。
_GUARD_STATS: Dict[str, int] = {
    "attempts": 0,               # 过了长度/语种门、进入 LLM 档决策的文本数
    "accepted": 0,               # 改写被采用（过全部守卫）
    "cache_hits": 0,             # 内存/落盘缓存命中（零 LLM）
    "skipped_interrogative": 0,  # 问句主导跳过（2026-08-10 答话式事故的源头规避）
    "cooldown_skips": 0,         # 熔断冷却期直接回落
    "rejected_sanitize": 0,      # 长度/语种/锚点/元话语拒
    "rejected_semantic": 0,      # 语义漂移拒（cos < 阈值）
    "rejected_unverifiable": 0,  # 语义校验不成立拒（fail-closed）
    "no_output": 0,              # 端点无产出/原样返回
}
_GS_LOCK = threading.Lock()


def _bump(key: str) -> None:
    with _GS_LOCK:
        _GUARD_STATS[key] = _GUARD_STATS.get(key, 0) + 1


# ── 端点熔断（连续失败 → 冷却期直接回落，不让挂掉的端点每条拖满超时）──────────
_FAIL_THRESHOLD = 3
_COOLDOWN_SEC = 60.0
_fail_streak = 0
_cooldown_until = 0.0
_last_ok_ts = 0.0     # 最近一次成功改写（wall clock，健康信号用）
_last_fail_ts = 0.0   # 最近一次失败（wall clock，健康信号用）
_CB_LOCK = threading.Lock()


def _in_cooldown() -> bool:
    with _CB_LOCK:
        return time.monotonic() < _cooldown_until


def _record_success() -> None:
    global _fail_streak, _cooldown_until, _last_ok_ts
    with _CB_LOCK:
        _fail_streak = 0
        _cooldown_until = 0.0
        _last_ok_ts = time.time()


def _record_failure() -> None:
    global _fail_streak, _cooldown_until, _last_fail_ts
    with _CB_LOCK:
        _fail_streak += 1
        _last_fail_ts = time.time()
        if _fail_streak >= _FAIL_THRESHOLD:
            _cooldown_until = time.monotonic() + _COOLDOWN_SEC
            logger.info("[voice_colloquial_llm] 连续 %d 次失败 → 冷却 %ds（回落规则档）",
                        _fail_streak, int(_COOLDOWN_SEC))


def health_signal() -> dict:
    """健康信号快照（HealthWatchdog 巡检消费，2026-07-15 九连败静默事故修复）。

    此前端点长期挂掉只会「每 60s 熔断-重试」无限循环，日志有记录但无人被通知，
    语音口语化静默降级规则档。信号口径与 avatar_voice_stats.hang_signal 对齐：
    连败数 + 最近成/败时刻，阈值判断留在 watchdog（可配置、可测试）。
    """
    with _CB_LOCK:
        sig = {
            "fail_streak": int(_fail_streak),
            "in_cooldown": time.monotonic() < _cooldown_until,
            "last_ok_ts": float(_last_ok_ts),
            "last_fail_ts": float(_last_fail_ts),
        }
    with _EP_LOCK:
        sig["providers"] = {
            k: dict(v) for k, v in sorted(_PROVIDER_STATS.items())}
        sig["last_provider"] = _last_provider
        now = time.monotonic()
        sig["endpoints_cooling"] = sorted(
            k for k, until in _EP_BAD_UNTIL.items() if until > now)
    sig["disk_cache_hits"] = int(_disk_hits)
    with _GS_LOCK:
        sig["guard_stats"] = dict(_GUARD_STATS)
    return sig


# ── 本地 AIClient 懒加载（默认关，开了才构造一次；测试可注入）──────────────────
_AI_CLIENT: Optional[Any] = None
_AI_LOCK = threading.Lock()


def _get_ai_client(injected: Optional[Any] = None) -> Optional[Any]:
    global _AI_CLIENT
    if injected is not None:
        return injected
    if _AI_CLIENT is None:
        with _AI_LOCK:
            if _AI_CLIENT is None:
                try:
                    from src.ai.ai_client import AIClient
                    from src.utils.config_manager import ConfigManager
                    _AI_CLIENT = AIClient(ConfigManager())
                except Exception as exc:
                    logger.warning("[voice_colloquial_llm] AIClient 懒加载失败: %s", exc)
                    return None
    return _AI_CLIENT


# 语义地板：改写与原文的嵌入余弦下限。2026-07-28 实测校准（bge-m3，9 对样本）：
# 真口语化 0.862~0.986（最低是「因此我们无需担心→所以咱不用担心」这种整句换词），
# 漂移 0.526~0.696（答非所问 0.684 / 换主题 0.526 / 回答式 0.696）。
# 2026-08-10 由 0.78 上调 0.84：当日生产拦下的答话式漂移已爬到 0.746/0.774，
# 漏网出站的那条（问句被答掉）在 0.78 之上——答话与原问同话题，余弦天然偏高，
# 0.78 离漂移带太近。0.84 仍低于真改写带下沿 0.862（不误杀），被拒的代价只是
# 回落规则档原文照念。刻意**不**指望余弦抓数字篡改（「198→168」余弦 0.974，
# 语义几乎不变），那半边由 lost_anchors 确定性兜住，两层各管一半。
DEFAULT_MIN_SIMILARITY = 0.84


async def _semantic_verdict(
    client: Any, original: str, rewritten: str, min_similarity: float,
) -> Tuple[bool, bool]:
    """语义校验裁决 → (通过, 校验成立)。

    **fail-closed**（2026-08-10 反转，原 fail-open）：LLM 改写是非确定性的，
    「没校验成」（无 embed / 端点抖动 / 返空）时旧行为直接放行——嵌入端点抖动
    的窗口恰好成了答话式漂移的出站通道（当日实录）。现在校验不成立一律不放行
    LLM 稿；调用方回落规则档＝原文照念，代价近零，换来硬不变量：
    **未经语义校验的非确定性改写永不出站**。
    ``min_similarity <= 0``＝运营显式关闸 → (True, True)（保留一键回旧行为）。
    """
    if min_similarity <= 0:
        return True, True
    embed = getattr(client, "embed", None) if client is not None else None
    if embed is None:
        return False, False
    try:
        vecs = await embed([original, rewritten])
        if not vecs or len(vecs) < 2 or not vecs[0] or not vecs[1]:
            return False, False
        va, vb = vecs[0], vecs[1]
        num = sum(x * y for x, y in zip(va, vb))
        na = sum(x * x for x in va) ** 0.5
        nb = sum(x * x for x in vb) ** 0.5
        if not na or not nb:
            return False, False
        sim = num / (na * nb)
    except Exception as exc:
        logger.info(
            "[voice_colloquial_llm] 语义校验不可用 → 拒用 LLM 稿（fail-closed）: %s",
            exc)
        return False, False
    if sim < float(min_similarity):
        logger.info(
            "[voice_colloquial_llm] 拒改写：语义漂移 cos=%.3f < %.2f | 原=%r 改=%r",
            sim, min_similarity, original[:30], rewritten[:30])
        return False, True
    return True, True


async def _semantically_same(
    client: Any, original: str, rewritten: str, min_similarity: float,
) -> bool:
    """兼容壳（门禁按此名引用）：fail-closed 语义下的布尔视图。"""
    ok, checked = await _semantic_verdict(client, original, rewritten,
                                          min_similarity)
    return ok and checked


async def llm_colloquialize(
    text: str,
    *,
    ai_client: Optional[Any] = None,
    emotion: str = "neutral",
    lead: bool = True,
    style: str = "",
    min_chars: int = 12,
    timeout_sec: float = 8.0,
    max_expand: float = 1.8,
    disfluency: bool = False,
    intensity: str = "natural",
    provider: str = "local",
    llm_endpoints: Any = None,
    temperature: float = 0.5,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
    script: bool = False,
) -> Optional[str]:
    """本地 LLM 口语化。命中缓存直接返回；失败/超时/熔断/校验不过 → None（回落规则档）。

    短句（<min_chars）/ 非中文 → None（与规则档同口径 no-op）。
    ``disfluency``＝本条允许一处轻口误自纠（⑥，调用方按 crc 低频开启；进缓存键）。
    ``intensity``＝口语重塑力度 light|natural|vivid（AI Live OS Agent6）：vivid 允许主观
    口吻框架/停顿，输出会比原文长一些 → 自动放宽长度上限（红线仍由 sanitize 守）。
    ``llm_endpoints``＝口语化专属 LAN 端点列表（``colloquial.llm_endpoints``，
    2026-08-01）：配了就作为「local」档的实现（逐端点失败冷却 failover），
    不配＝沿用 ``ai.fallback``（client.rewrite_local），老部署行为不变。
    ``temperature``＝改写采样温度（默认 0.5——改写要保真，比对话档 0.7 收敛；
    进缓存键）。

    内容保持双层（2026-07-28 事故：本地模型把「嗨，我在宿务这边刚开完店，你吃饭了吗」
    改写成「刚忙完啊我吃过了你那边店开起来肯定一堆事吧…」＝**回答**而不是改写，
    38 字对 18 字刚好卡在 1.8 倍上限内一路放行，人设语音说出的话与要说的话完全无关）：
      ① ``sanitize_llm_output`` 里的 ``lost_anchors``——数字/金额/型号丢了即拒（确定性）；
      ② 本函数的 ``_semantically_same``——嵌入余弦 < ``min_similarity`` 即拒（fail-open）。
    两层都不过就回落规则档（原文照念），永远不劣化。
    """
    core = str(text or "").strip()
    if len(core) < max(1, int(min_chars)):
        return None
    from src.ai.voice_colloquial import _is_chinese_dominant
    if not _is_chinese_dominant(core):
        return None
    # 粤语语音稿不过 LLM 口语化（2026-08-30 粤语人设 verifier 抓缝）：粤文本身
    # 就是口语书写形，改写零增益；而改写 prompt 只约束「同一种语言」、消毒器
    # _is_chinese_dominant 分不出粤/普、语义地板拦不住同义「粤转普」——粤文会被
    # 磨回普通话书写，再由 zh-HK 音色念出＝失地道感。skip 即保真（规则档词表
    # 对粤文几乎零命中，天然无害，不必拦）。
    try:
        from src.ai.lang_voice_route import is_cantonese_text
        if is_cantonese_text(core, min_markers=2):
            return None
    except Exception:
        pass
    _bump("attempts")
    if is_interrogative_dominant(core):
        _bump("skipped_interrogative")
        return None      # 问句主导：跳过 LLM 档（见 is_interrogative_dominant 注释）

    # vivid 档加了主观口吻框架，天然更长 → 放宽膨胀上限（≤1.8 会误杀正常重塑）。
    if str(intensity or "").strip().lower() == "vivid" and max_expand < 2.2:
        max_expand = 2.2
    # 剧本档：‖/[breath] 等标记额外占字，再放宽（事实红线由 sanitize 的锚点守卫兜）
    if script and max_expand < 2.6:
        max_expand = 2.6

    key = _cache_key(core, emotion, bool(lead), style, bool(disfluency),
                     (f"script{_SCRIPT_PROMPT_VERSION}#" + intensity)
                     if script else intensity,
                     temperature)
    hit = _cache_get(key)
    if hit is not None:
        _bump("cache_hits")
        return hit or None          # 缓存空串＝已知无有效改写 → 回落规则档
    disk_hit = _disk_get(key)
    if disk_hit:
        _bump("cache_hits")
        _cache_put(key, disk_hit)   # 落盘命中 → 回填内存层（跨重启零 LLM）
        return disk_hit

    if _in_cooldown():
        _bump("cooldown_skips")
        return None                 # 端点冷却期：秒回落，不调 LLM

    eps = parse_llm_endpoints(llm_endpoints)
    client = _get_ai_client(ai_client)
    if client is None and not eps:
        return None

    # provider：local=LAN 本地模型（llm_endpoints 或 ai.fallback，零云成本）｜
    # cloud=主云端模型（质量稳但每条一次调用+云成本）｜auto=云端优先、失败回落本地｜
    # local_first=LAN 优先、全端点失败才落云端（「176 的逻辑」生产档：LAN 修好自动
    # 回归零云成本，LAN 挂了云端兜底不掉规则档）。
    prov = str(provider or "local").strip().lower()
    order = {
        "cloud": ("cloud",),
        "auto": ("cloud", "local"),
        "local_first": ("local", "cloud"),
    }.get(prov, ("local",))
    if script:
        system = build_speech_script_prompt(emotion, style,
                                            disfluency=bool(disfluency))
    else:
        system = build_colloquial_prompt(emotion, bool(lead), style,
                                         disfluency=bool(disfluency),
                                         intensity=intensity)
    raw = None
    for _p in order:
        if raw:
            break
        if _p == "cloud":
            _fn = getattr(client, "rewrite_cloud", None) if client else None
            if _fn is None:
                continue
            try:
                raw = await _fn(system, core, timeout_sec=timeout_sec,
                                temperature=temperature)
            except Exception as exc:
                logger.debug("[voice_colloquial_llm] rewrite_cloud 异常: %s", exc)
                raw = None
            _note_provider("cloud", bool(raw))
            continue
        # local 档：端点列表优先（逐端点冷却 failover），未配则沿用 ai.fallback
        if eps:
            for ep in eps:
                ep_key = _endpoint_key(ep)
                if _ep_in_cooldown(ep_key):
                    continue
                try:
                    raw = await _rewrite_via_endpoint(
                        ep, system, core, temperature=temperature,
                        max_tokens=400 if script else 240)
                except Exception as exc:
                    logger.debug("[voice_colloquial_llm] 端点 %s 异常: %s",
                                 ep_key, exc)
                    raw = None
                _note_provider(ep_key, bool(raw))
                if raw:
                    break
        else:
            _fn = getattr(client, "rewrite_local", None) if client else None
            if _fn is None:
                continue
            try:
                raw = await _fn(system, core, timeout_sec=timeout_sec,
                                temperature=temperature)
            except Exception as exc:
                logger.debug("[voice_colloquial_llm] rewrite_local 异常: %s", exc)
                raw = None
            _note_provider("fallback", bool(raw))

    if not raw:
        out = None
    elif script:
        out = sanitize_speech_script(raw, core, max_expand=max_expand)
    else:
        out = sanitize_llm_output(raw, core, max_expand=max_expand)
    if out and out != core:
        # 剧本档语义比对用剥标记后的纯文本（标记是韵律指令，不是语义内容）
        _cmp = strip_speech_script(out) if script else out
        ok, checked = await _semantic_verdict(client, core, _cmp, min_similarity)
        if not checked:
            # 嵌入侧不可用：拒用本次 LLM 稿（fail-closed），但**不投毒缓存、
            # 不推熔断**——这不是这句话或改写端点的错，嵌入恢复后同句应立即可用。
            _bump("rejected_unverifiable")
            return None
        if not ok:
            _bump("rejected_semantic")
            _record_failure()
            _cache_put(key, "")
            return None
        _bump("accepted")
        _record_success()
        _cache_put(key, out)
        _disk_put(key, out)
        return out
    # 校验不过 / 空 / 与原文相同：记一次失败（推进熔断），内存缓存空串短期不重试
    # 同句；刻意**不落盘**——临时故障不该固化成「这句永远不改写」。
    _bump("rejected_sanitize" if (raw and out is None) else "no_output")
    _record_failure()
    _cache_put(key, "")
    return None


def set_ai_client(client: Optional[Any]) -> None:
    """注入已初始化的主 AIClient（main.py bootstrap 调用）。

    修复（2026-07-24）：本模块原靠 ``_get_ai_client`` 懒建 ``AIClient(ConfigManager())``，
    但该 ConfigManager 从未 ``await load()``、AIClient 从未 ``await initialize()``
    → ``_fb_model`` 恒空 → ``rewrite_local`` 恒 None → **colloquial LLM 档实际从未生效**
    （静默回落规则档）。main.py 在 ``ai_client.initialize()`` 后注入已初始化实例，
    口语化 LLM 档才真正走 ``ai.fallback`` 本地端点。未注入时仍回落原懒加载（兼容测试）。
    """
    global _AI_CLIENT
    with _AI_LOCK:
        _AI_CLIENT = client


def reset_state(*, disk: bool = True) -> None:
    """清空缓存 + 熔断 + 端点冷却 + 懒加载客户端（测试用）。

    ``disk=False``：保留落盘缓存（测「跨重启复用」时只清内存层模拟进程重启）。
    """
    global _AI_CLIENT, _fail_streak, _cooldown_until, _last_ok_ts, _last_fail_ts
    global _last_provider, _disk_hits
    with _GS_LOCK:
        for _k in list(_GUARD_STATS):
            _GUARD_STATS[_k] = 0
    with _CACHE_LOCK:
        _CACHE.clear()
    with _CB_LOCK:
        _fail_streak = 0
        _cooldown_until = 0.0
        _last_ok_ts = 0.0
        _last_fail_ts = 0.0
    with _EP_LOCK:
        _EP_BAD_UNTIL.clear()
        _PROVIDER_STATS.clear()
        _last_provider = ""
    _disk_hits = 0
    with _AI_LOCK:
        _AI_CLIENT = None
    if disk:
        try:
            import os
            path = _disk_path()
            if path and os.path.exists(path):
                os.unlink(path)
        except Exception:
            pass


__all__ = [
    "DEFAULT_MIN_SIMILARITY",
    "llm_colloquialize", "set_ai_client", "build_colloquial_prompt", "sanitize_llm_output",
    "lost_anchors", "food_terms", "introduced_food_terms",
    "is_interrogative_dominant", "health_signal", "reset_state",
    "parse_llm_endpoints", "get_last_provider",
]
