"""Stage A：陪伴「形象照/自拍」引擎（对标星野/Talkie/Replika 招牌能力）。

把变现目录里早已定义、却**零交付代码**的付费项 `exclusive_album`（专属相册）真正"通电"：
用户在对话里要照片 → 按关系等级 + 付费权益判准入 → 生成在 persona 一致的形象照 → 发出；
够不着的（关系浅）温柔搪塞，未解锁的给软付费引导（驱动 exclusive_album 转化）。

本模块是**纯逻辑 + 软失败 provider 骨架**（镜像 `tts_pipeline` 范式）：意图识别 / 提示词构造 /
准入决策都是可单测纯函数；图像 provider 默认 `disabled`（不接真模型零行为），接 openai images /
本地命令模板（如 ComfyUI/SD 推理脚本）后才真正出图。绝不抛——任何失败退回文字陪伴。

安全：提示词强制 SFW 约束（成人/暴露内容硬约束在 prompt 层，配合 persona_guard/wellbeing）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import shlex
import subprocess
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Pattern, Tuple

from src.utils.monetization import feature_allowed

logger = logging.getLogger(__name__)

# 付费项 id（变现目录 items.exclusive_album）；自拍超出免费额度后据此判拥有/引导解锁。
SELFIE_FEATURE = "exclusive_album"

# 意图关键词（多语，刻意保守——须明确指向"对方/AI 的样子/照片"，避免误命中用户自述照片）。
_REQUEST_MARKERS = (
    "自拍", "你的照片", "你的相片", "你的样子", "你长什么样", "你长啥样",
    "拍张照", "拍张自拍", "拍一张", "发张照片", "发张自拍", "发张图", "发个自拍",
    "看看你", "想看你", "你的照", "来张照片", "给我看看你", "你的写真", "你的近照",
    "你近照", "妳近照", "妳的近照",
    # 2026-07-14 真机验收漏报补齐："再给我发一遍新照片，不要老照片"一个 marker 都没
    # 命中 → AI 嘴上答应发图实际装死。"新照片/近照"是强索图词，单独出现基本都在要图
    # （客户谈自己的新照片被误发一张自拍属可接受的便宜错误，方向反了也不崩人设）。
    "新照片", "近照", "新自拍", "新的自拍", "最新的照片", "现在的照片",
    "再来一张", "再发一张", "再拍一张", "再來一張", "再發一張", "再拍一張",
    # 2026-07-22 真机漏报补齐：首图发出后客户换量词/口语追要，一个都没命中 →
    # AI 顺话题空谈还幻觉"发了呀"。追要/追问句是高频索图形态（"张照片/个照片"
    # 单独收录会误伤"我拍了张照片"类叙述，不收）。
    "再拍一个", "再拍个", "再来一个", "再发一个", "再给我一个", "再给我一张",
    "再來一個", "再發一個", "再給我一個", "再給我一張",
    "给我照片", "給我照片", "给我你的照片", "給我你的照片",
    # 量词插入漏报（2026-07-22 TG 真机）：「给我个照片」不含连续子串「给我照片」。
    "给我个照片", "給我個照片", "给我一张照片", "給我一張照片",
    "给我张照片", "給我張照片", "给我个相片", "給我個相片",
    "发个照片", "發個照片", "发张相", "發張相", "来个照片", "來個照片",
    "照片呢", "图呢", "圖呢", "照片没看到", "照片沒看到", "没收到照片", "沒收到照片",
    # 实施69 实录漏网（2026-08-24 夜市摊事故）：「拍个照我看看」「拍照烤串的
    # 给我看看」两次显式要图零命中 → 图链全程不启动，LLM 独自空转出谎言。
    # 量词+裸「照」只收「拍X照」结构（「拍照」二字单独出现是动词泛指不收，
    # 防「我喜欢拍照」误伤）；「拍照…给我看」语序走下方 _REQUEST_PATTERNS。
    "拍个照", "拍個照", "拍张照", "拍張照", "拍一张照", "拍一張照",
    # 繁体 / 港台常见写法（对方多为繁体输入，简体 marker 匹配不到 → 补齐同义）
    "發張照片", "發個照片", "發張自拍", "發個自拍", "發張圖", "傳張照片", "傳個照片",
    "拍個照片", "來張照片", "給我看看你", "給我看看妳", "看看妳", "想看妳",
    "你的樣子", "妳的照片", "妳的相片", "妳的樣子", "你長什麼樣", "你長啥樣",
    "妳長什麼樣", "你的寫真", "你的近照",
    # 粤语（2026-07-22 WA 真机）：「我要你嘅相片 / 你个相啊」简繁词表零命中。
    # 不收裸「相啊」（会误伤「互相啊」）；靠「个相啊 / 你个相」+ 下方正则。
    "你嘅相片", "你嘅照片", "你嘅相", "妳嘅相片", "妳嘅照片",
    "你个相", "你個相", "妳个相", "妳個相", "个相啊", "個相啊",
    "睇下你相", "睇下你个相", "睇下你個相", "睇睇你相",
    "有相未", "有照片未", "整张相", "整張相", "俾张相", "俾張相", "俾个相", "俾個相",
    # 闽南语常见书面/ASR 落字（说话用闽南、打字常混普通话字）
    "欲看你的相", "欲看你的照", "要看你的相", "看你的相",
    "你的相拿来", "你的相拿來", "寄张照片", "寄張照片", "寄个照片", "寄個照片",
    "selfie", "photo of you", "pic of you", "picture of you", "send a pic",
    "send me a pic", "send a photo", "show me you", "show me your face",
    "what do you look like", "your photo", "your picture", "see your face",
    "your recent photo", "recent photo of you", "latest photo of you",
    "new photo", "new pic", "another photo", "another pic", "one more photo",
    "one more pic",
    # 日/韩/西/葡/泰/越常见索图
    "写真送", "自撮り", "顔見せ", "顔を見せ",
    "사진 보내", "셀카", "셀피", "얼굴 보여",
    "tu foto", "sua foto", "una selfie", "manda una foto", "manda a foto",
    "envía una foto", "envia uma foto",
    "ส่งรูป", "รูปคุณ", "รูปเธอ",
    "gửi ảnh", "gửi hình", "ảnh của bạn", "hình của bạn",
)

# 反向护栏：明确指向"对方做/煮/买的东西"的照片（"你煮的…拍张照给我看"）属**对话临时要图**
# （上下文要图 = 后续 Stage B），不是"人设本人自拍" → 命中则不当作 selfie，避免误发人设照片。
_OBJECT_PHOTO_MARKERS = (
    "你煮的", "妳煮的", "你做的", "妳做的", "你买的", "你買的",
    "你拍的", "你點的", "你点的", "你種的", "你种的", "你養的", "你养的",
    "你寫的", "你写的", "你画的", "你畫的",
    # 粤语同义
    "你煮嘅", "妳煮嘅", "你整嘅", "你買嘅", "你买嘅",
)

# EN 物体图（#171 同批）：「picture of your dog / photo of the food」是要
# 「你的东西」的图，不是人设自拍——旧子串 marker「picture of you」会吃进
# 「picture of your dog」（前缀重合）。「of your face / of yourself」仍是自拍。
_OBJECT_PHOTO_RE_EN = re.compile(
    r"\b(?:photos?|pics?|pictures?|shots?)\s+of\s+"
    r"(?:your|ur|the|that|this|my|his|her|their)\s+(?!face\b|self\b|selfie\b)\w+",
    re.I,
)

# 用户谈自己的照片：无指向 AI 领属时不命中（防「我给你看我的照片」）。
# 实施69 追加自述动作组（「我去拍个照」是用户叙述自己要去拍照，不是索图——
# 新收的「拍个照」marker 若无此护栏会把它误判成要图）。
_OWN_PHOTO_GUARD = (
    "我的照片", "我的相片", "我嘅照片", "我嘅相片", "我拍了", "我拍的照片",
    "给你看我的", "給你看我的", "我发的照片", "我發的照片", "看我的照片",
    "我的自拍", "發我的照片", "发我的照片",
    "我去拍", "我拍个", "我拍個", "我拍张", "我拍張", "我来拍", "我來拍",
    "我要拍", "我先拍",
)

# 结构化索图（量词/方言插入后纯子串不够）：要/发/睇 + 可选领属 + 相/照片。
# 裸「相」只收 个相/嘅相/的相/张相/相啊 等，避开「相信/相关/互相」。
_PHOTO_CORE = (
    r"(?:自拍|相片|照片|近照|写真|寫真|"
    r"个相|個相|嘅相|的相|张相|張相|相啊|相呀)"
)
_YOUR_POS = r"(?:你的|妳的|你嘅|妳嘅|你个|你個|妳个|妳個|your\s+)?"
_REQUEST_PATTERNS: Tuple[Pattern[str], ...] = (
    # 给我个照片 / 发张自拍 / 要你嘅相片 / 睇下你个相 / send me a photo
    re.compile(
        r"(?:给我|給我|发|發|传|傳|寄|拍|来|來|要|想要|整|俾|"
        r"睇下|睇睇|看下|看看|想看|想睇|"
        r"甲我|乎我|予我|共我|"
        r"show\s*me|send\s*(?:me\s*)?)"
        r".{0,14}"
        + _YOUR_POS
        + _PHOTO_CORE,
        re.I,
    ),
    # 领属在前：你嘅相片 / 你个相 / your photo
    re.compile(
        r"(?:你的|妳的|你嘅|妳嘅|你个|你個|妳个|妳個|your\s+)"
        + _PHOTO_CORE,
        re.I,
    ),
    # 实施69：「拍(照)X给我看看」动词前置语序（「拍照烤串的给我看看」）。
    # 主体是什么交 wanted_media_subject 判，这里只负责认出「这是要图」。
    re.compile(r"拍照?[^，。！？!?\n]{0,10}(?:给|給|俾)\s*我\s*(?:看|睇)"),
    # #171（2026-09-05 WhatsApp 英文客户实录）：EN 索图此前只靠上面的子串表
    # （send me a pic / photo of you…），_PHOTO_CORE 全是中文名词——「send me a
    # photo」「show me a picture」「can I see a pic」「got any pics」这些最常见
    # 英文形态一个都进不来 → 图链不启动，LLM 独自空转出「I just sent it」。
    # ① send/show/shoot/drop (me) a photo|pic|picture|selfie（排除「picture of
    #    your dog/the food」——那是物体图，交上下文要图链）
    re.compile(
        r"\b(?:send|show|shoot|drop|gimme|give)\s+(?:me\s+)?"
        r"(?:a|an|another|one\s+more|some|ur|your|the|that|dat)\s+"
        r"(?:pics?|photos?|pictures?|selfies?|face)\b"
        r"(?!\s+of\s+(?!(?:you|u|yourself|ya)\b))",
        re.I,
    ),
    # ② can/could I see (you | your face | what you look like | a pic)——
    #    排除「can I see you tonight/tomorrow/in person」这类约见面语义
    re.compile(
        r"\b(?:can|could|may)\s+i\s+(?:please\s+)?see\s+"
        r"(?:(?:a|another|one\s+more|some)\s+(?:pics?|photos?|pictures?|selfies?)|"
        r"your\s+face|what\s+(?:you|u)\s+look\s+like|(?:you|u))\b"
        r"(?!\s+(?:tonight|tomorrow|today|later|soon|again|this|next|in\s+person|irl|"
        r"at|on|after|before|when|sometime|for|there|then|now|one\s+day|someday))",
        re.I,
    ),
    # ③ got any pics? / do you have any photos / any selfies?——裸「got photos」
    #    不收（「I got photos from my trip」是客户自述），必须带 any/more/new 限定
    re.compile(
        r"\b(?:(?:got|do\s+you\s+have|you\s+got|u\s+got|you\s+have|u\s+have)\s+"
        r"(?:any\s+|more\s+|other\s+|new\s+|some\s+)|any\s+(?:more\s+|other\s+|new\s+)?)"
        r"(?:pics?|photos?|pictures?|selfies?)\b"
        r"(?!\s+of\s+(?!(?:you|u|yourself|ya)\b))",
        re.I,
    ),
    # ④ let me / lemme see you / your face / what you look like
    re.compile(
        r"\b(?:let\s+me|lemme)\s+see\s+(?:you|u|your\s+face|what\s+(?:you|u)\s+look\s+like)\b"
        r"(?!\s+(?:tonight|tomorrow|today|later|soon|again|in\s+person|irl))",
        re.I,
    ),
    # ⑤ what (do) you look like / how do you look
    re.compile(
        r"\bwhat\s+(?:do\s+|does\s+)?(?:you|u)\s+look\s+like\b|\bhow\s+do\s+(?:you|u)\s+look\b",
        re.I,
    ),
    # 闽南「甲我看…相」
    re.compile(
        r"(?:甲我|乎我|予我|共我).{0,8}(?:看|睇).{0,10}" + _PHOTO_CORE
    ),
    # 日
    re.compile(
        r"(?:写真|自撮り).{0,6}(?:送|見せ|くれ)|(?:顔|かお).{0,4}見せ"
    ),
    # 韩
    re.compile(
        r"(?:사진|셀카|셀피).{0,8}(?:보내|보여)|얼굴\s*보여"
    ),
    # 西/葡
    re.compile(
        r"(?:manda|env[ií]a|envia).{0,10}(?:foto|selfie)|(?:tu|sua)\s+foto",
        re.I,
    ),
    # 泰 / 越
    re.compile(r"ส่ง\s*รูป|รูป(?:คุณ|เธอ)|เซลฟ[ีิ]"),
    re.compile(
        r"(?:gửi|cho)\s*(?:ảnh|hình)|(?:ảnh|hình)\s*của\s*bạn",
        re.I,
    ),
)


def detect_selfie_request(text: str) -> bool:
    """是否在向 AI 索要形象照/自拍（多语/方言，含繁体与粤语/闽南常见写法）。

    反向护栏：请求明确指向"对方做/煮/买的东西"的照片（``你煮的…拍张照``）→ 返回 False，
    交由上下文要图路径处理，避免把"拍下你煮的面"误当成"发一张你的自拍"。
    另：用户谈自己的照片且无「你的/你嘅」领属 → False。
    """
    t = str(text or "").strip().lower()
    if not t or len(t) > 200:  # 超长多半是叙述而非索图
        return False
    if any(m in t for m in _OBJECT_PHOTO_MARKERS):
        return False
    if _OBJECT_PHOTO_RE_EN.search(t):
        return False
    if any(g in t for g in _OWN_PHOTO_GUARD):
        if not any(x in t for x in (
            "你的", "妳的", "你嘅", "妳嘅", "你个", "你個", "your",
        )):
            return False
    # Q-36（#313 2JK95C）：客户**提议发自己的图**（「一会我拍照片给你看」「I sent a photo, did you
    # see it?」六语）不是索图——上面的子串护栏只收固定词，「我拍照片」「我发张我的」都漏；
    # 共享判定在 commitment_guard.detect_offer_media（带「你的 / your」混合句它自己返回空，索要照旧）。
    try:
        from src.inbox.commitment_guard import detect_offer_media
        if detect_offer_media(text):
            return False
    except Exception:
        pass
    if any(m in t for m in _REQUEST_MARKERS):
        return True
    return any(p.search(t) for p in _REQUEST_PATTERNS)


def _persona_visual(persona: Any) -> str:
    """从 persona（dict/str）抽取**真实外貌描述**（不含 name 兜底）；缺则空串。

    str 形态护栏：调用方（skill_manager）在 persona dict 拿不到时会回传**人设名字符串**
    （如"林小雨"）——短且无分隔符的纯 CJK 串按名字论、不当外貌描述（名字对生图模型是
    噪声，是 2026-07-13 狗图事故的帮凶之一）；真正的中文外貌描述通常含逗号/空格。
    """
    if isinstance(persona, str):
        s = persona.strip()
        if (_has_cjk(s) and len(s) <= 10
                and not any(sep in s for sep in (" ", ",", "，", "、", ";", "；"))):
            return ""
        return s
    if not isinstance(persona, dict):
        return ""
    for k in ("appearance", "visual", "look", "self_image_desc", "description"):
        v = str(persona.get(k) or "").strip()
        if v:
            return v
    return ""


def _persona_name(persona: Any) -> str:
    return str(persona.get("name") or "").strip() if isinstance(persona, dict) else ""


def _has_cjk(s: str) -> bool:
    return any("\u4e00" <= c <= "\u9fff" for c in str(s or ""))


def _persona_gender_word(persona: Any) -> str:
    """从 persona 推断主体性别词（woman/man）；推不出返回空串。gender 字段优先，tags 兜底。"""
    if not isinstance(persona, dict):
        return ""
    g = str(persona.get("gender") or "").strip().lower()
    if g in ("female", "f", "woman", "女", "女性"):
        return "woman"
    if g in ("male", "m", "man", "男", "男性"):
        return "man"
    tags = {str(t).strip() for t in (persona.get("tags") or [])}
    if tags & {"女性", "女"}:
        return "woman"
    if tags & {"男性", "男"}:
        return "man"
    return ""


def _persona_smart_base(persona: Any) -> str:
    """persona 无外貌字段时，从结构化字段（gender/age/中文名）推一个**明确是人类**的主体描述。

    事故背景（2026-07-13）：旧兜底 ``a warm, friendly companion named 林小雨`` 中
    "companion" 在英文生图语料里强关联 companion animal（陪伴犬），且中文名对
    FLUX 的 CLIP/T5 编码器无任何约束 → 实际生成了一条穿黄毛衣的狗发给客户。
    因此这里**绝不把非拉丁名字塞进 prompt**，只输出 "a 22-year-old East Asian woman"
    这类模型能硬约束的人类主体描述。
    """
    if not isinstance(persona, dict):
        return ""
    subject = _persona_gender_word(persona) or "person"
    eth = "East Asian " if _has_cjk(_persona_name(persona)) else ""
    try:
        age_i = int(persona.get("age") or 0)
    except Exception:
        age_i = 0
    if 5 <= age_i <= 100:
        return f"a {age_i}-year-old {eth}{subject}, warm friendly expression"
    return f"a young {eth}{subject}, warm friendly expression"


# ── 「真人感」多样性池（治头位置/表情千篇一律）───────────────────────────────
# 背景：PuLID 从基准正面照锁脸 + 固定种子 + prompt 恒为「looking at the camera」
# 三重叠加 → 每张构图/头部朝向/表情雷同，像证件照连拍。这里提供**可轮换**的取景/
# 头部姿态/视线/表情/写实质感词池，由 ``selfie_variety`` 按 salt 确定性组合注入
# prompt。**刻意保守 SFW**：只收景别/角度/神态/生活化质感，不收衣着/体态类（那类
# 易滑向擦边，交由 SFW 硬约束）。保留「solo, one person」单人锚（防多人/非人跑偏，
# 狗图事故教训），只让「看镜头」这一条随池轮换。
_VARIETY_FRAMING = (
    "close-up selfie", "half-body shot", "waist-up framing",
    "candid photo with slightly off-center framing", "mirror selfie",
    "arm's-length selfie", "cozy indoor snapshot",
)
_VARIETY_HEAD = (
    "head tilted slightly", "three-quarter face angle", "gentle side profile",
    "facing forward naturally", "chin lifted a little", "leaning cheek on one hand",
)
_VARIETY_GAZE = (
    "looking at the camera", "looking away to the side", "glancing off-frame",
    "eyes softly downcast", "looking back over the shoulder",
    "gazing into the distance",
)
_VARIETY_EXPR = (
    "soft natural smile", "cheerful laugh with teeth showing",
    "relaxed calm expression", "playful little smirk",
    "slightly surprised, eyebrows raised", "warm gentle smile",
    "candid mid-laugh, eyes crinkled",
)
_VARIETY_REALISM = (
    "casual smartphone snapshot", "natural skin texture with subtle imperfections",
    "amateur candid photo, unposed", "soft phone-camera look with faint grain",
    "everyday casual photo, slightly imperfect focus",
)


def selfie_variety(salt: Any) -> Dict[str, str]:
    """按 ``salt`` 确定性地从取景/头部姿态/视线/表情/写实池各取一项（纯函数）。

    各池用不同的整除步长错开轮换（避免五项锁步一起变），同 salt 恒定可复现、
    缓存友好。返回 ``{framing, head, gaze, expr, realism}``；``build_selfie_prompt``
    据此拼多样性修饰词。非数字/异常 salt 归一为 0（返回各池首项）。
    """
    try:
        s = abs(int(salt))
    except Exception:
        s = 0
    return {
        "framing": _VARIETY_FRAMING[s % len(_VARIETY_FRAMING)],
        "head": _VARIETY_HEAD[(s // 7) % len(_VARIETY_HEAD)],
        "gaze": _VARIETY_GAZE[(s // 13) % len(_VARIETY_GAZE)],
        "expr": _VARIETY_EXPR[(s // 17) % len(_VARIETY_EXPR)],
        "realism": _VARIETY_REALISM[(s // 23) % len(_VARIETY_REALISM)],
    }


def resolve_variety_salt(scfg: Any, *, enabled_default: bool = False) -> Optional[int]:
    """按 ``companion.selfie.variety.enabled`` 决定本次出图的多样性 salt（治千篇一律）。

    返回随机 int（每次发送不同 → 姿态/表情/构图各异）或 None（关闭 → 旧行为）。
    默认关（``enabled_default=False``），线上经 overlay opt-in。软失败返回 None（不阻断出图）。
    调用方把它同时喂 ``build_selfie_prompt(variety_salt=)`` 与
    ``stable_selfie_seed(key, salt=)``，使 prompt 与底噪一起变化。
    """
    try:
        vc = (scfg or {}).get("variety") if isinstance(scfg, dict) else None
        on = (bool(vc.get("enabled", enabled_default)) if isinstance(vc, dict)
              else bool(enabled_default))
        if not on:
            return None
        return random.randint(0, 2 ** 30)
    except Exception:
        return None


# 机器独占的角色 LoRA 注册表（训练→选优后由 persona_lora_eval --write-registry 原子写入；
# 用 JSON 而非改人工带注释的 YAML → 零风险不丢注释）。resolve 作「人设字段 > 注册表 > 全局」
# 中间优先级读取（mtime 缓存，出图路径低频调用零负担）。
DEFAULT_LORA_REGISTRY = "config/persona_lora.json"
_LORA_REG_LOCK = threading.Lock()
_LORA_REG_CACHE: Dict[str, Any] = {"path": None, "mtime": -1.0, "data": {}}


def _persona_key(persona: Any) -> str:
    """人设注册表键：dict 取 id/persona_id/name；str（外貌描述兜底）原样（基本不会命中注册表）。"""
    if isinstance(persona, dict):
        return str(persona.get("id") or persona.get("persona_id")
                   or persona.get("name") or "").strip()
    return str(persona or "").strip()


def load_lora_registry(path: Any = None) -> Dict[str, Any]:
    """读角色 LoRA 注册表 JSON（mtime 缓存；不存在/损坏 → {}）。纯读、绝不抛。"""
    p = str(path or DEFAULT_LORA_REGISTRY)
    try:
        mt = os.path.getmtime(p)
    except OSError:
        return {}
    with _LORA_REG_LOCK:
        if _LORA_REG_CACHE["path"] == p and _LORA_REG_CACHE["mtime"] == mt:
            return _LORA_REG_CACHE["data"]
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            data = data if isinstance(data, dict) else {}
        except Exception:
            data = {}
        _LORA_REG_CACHE.update(path=p, mtime=mt, data=data)
        return data


def resolve_persona_lora(
    persona: Any, scfg: Any, *, registry: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """解析该人设的**角色 LoRA spec**：``{file, weight, trigger}``。

    多人设各训各的 LoRA（不同文件/触发词），而出图 ``command_args`` 只有一条 → 靠这里按
    persona 解析、经 ``{lora}``/``{lora_weight}`` 占位与 prompt 触发词注入。
    **三层优先级**：① persona dict 的 ``lora_file``/``lora_trigger``/``lora_weight``
    （profiles_runtime.yaml 人工指定，最高）→ ② 注册表 ``config/persona_lora.json`` 的
    ``<pid>`` 项（训练选优后机器写回，deploy 免手改）→ ③ ``scfg['lora']`` 全局块
    （单人设/统一 LoRA 兜底）。``file`` 空＝不挂 LoRA（回落 PuLID）。字段级合并（各键独立取最高层）。
    ``registry`` 显式传入则用之（测试/批量用），否则自动加载默认注册表（mtime 缓存）。
    """
    lcfg = (scfg or {}).get("lora") if isinstance(scfg, dict) else None
    lcfg = lcfg if isinstance(lcfg, dict) else {}
    reg = registry if registry is not None else load_lora_registry()
    rentry = reg.get(_persona_key(persona)) if isinstance(reg, dict) else None
    rentry = rentry if isinstance(rentry, dict) else {}

    def _pick(pkey: str, rkey: str, ckey: str, default: Any = "") -> Any:
        if isinstance(persona, dict) and persona.get(pkey) is not None:
            return persona.get(pkey)
        if rentry.get(rkey) is not None:
            return rentry.get(rkey)
        v = lcfg.get(ckey)
        return v if v is not None else default

    file = str(_pick("lora_file", "file", "file", "") or "").strip()
    trigger = str(_pick("lora_trigger", "trigger", "trigger", "") or "").strip()
    wraw = _pick("lora_weight", "weight", "weight", 1.0)
    try:
        weight = float(wraw if wraw not in (None, "") else 1.0)
    except Exception:
        weight = 1.0
    return {"file": file, "weight": weight, "trigger": trigger}


# 尺度分级 clause（喂 FLUX 的**正面**措辞——cfg=1 无负面提示词，靠正面措辞钉死画面）：
#   sfw        —— 完全遮盖、日常向（默认、历史行为不变）；
#   suggestive —— 性感但**不露骨**：允许身材展现/暧昧姿态/低胸泳装内衣，
#                 但硬保留「不露点、不色情」底线（真正的露骨拦截由 image_gate 兜底）。
# 刻意**不提供 explicit 档**：露骨内容越过平台/法律线，产品层不开这个口子。
_RATING_CLAUSES = {
    "sfw": "fully clothed, tasteful, safe-for-work, no nudity",
    "suggestive": (
        "alluring, flirtatious and sensual, tasteful glamour, "
        "figure-flattering or form-fitting outfit, confident seductive expression, "
        "tasteful and not pornographic, no full nudity, no exposed intimate parts"
    ),
}


def normalize_content_rating(content_rating: str = "", *, sfw: bool = True) -> str:
    """归一化尺度档位（纯函数）：``sfw`` | ``suggestive`` | ``none``。

    ``content_rating`` 显式给了则优先（未知值 → 保守回落 sfw）；为空时由旧参数 ``sfw``
    推：``sfw=True`` → ``"sfw"``，``sfw=False`` → ``"none"``（保持历史「无约束」边界）。
    ``explicit`` 被刻意拦回 ``suggestive``——产品层不出露骨。
    """
    r = str(content_rating or "").strip().lower()
    if not r:
        return "sfw" if sfw else "none"
    if r in ("suggestive", "sensual", "spicy"):
        return "suggestive"
    if r in ("explicit", "nsfw", "porn"):
        return "suggestive"  # 露骨诉求一律降级到不露骨（硬护栏，不给 explicit 口子）
    return "sfw"


def album_series_of_path(path: Any) -> str:
    """从策展文件名解析系列：``<scene>_<series>_<nn>.jpg`` → series；不合式返回空。

    与 album_curator 的命名约定同源（scene 与 series 都是小写连字符 slug，
    末段是 2 位序号）。auto_selfie_* 等非策展名 → ""（只按整文件排除）。
    """
    stem = Path(str(path or "")).stem
    parts = stem.split("_")
    if len(parts) >= 3 and parts[-1].isdigit():
        return "_".join(parts[1:-1])
    return ""


def album_scene_class_of_path(path: Any) -> str:
    """从策展文件名解析场景类：``<scene>_<series>_<nn>.jpg`` → scene 段归一；
    不合式/认不出返回 ""（场景未知）。归一词表见 ``persona_media.scene_class_of``。"""
    stem = Path(str(path or "")).stem
    parts = stem.split("_")
    if len(parts) >= 3 and parts[-1].isdigit():
        from src.companion.persona_media import scene_class_of
        return scene_class_of(parts[0])
    return ""


# ── 相册目录元数据 sidecar（P0 图文一致性，2026-07-27）──────────────────────
# 每相册目录可放 ``_meta.json``：``{"<文件名>": {"tod": "day|night", "scene":
# "<场景类>", "series": "<系列>"}}``（由 scripts/persona_media_backfill_meta.py
# 用 VLM+文件名约定生成，运营也可手改）。运行时按 mtime 缓存；无 sidecar =
# 全部字段未知（向后兼容零行为变更）。
_ALBUM_META_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
ALBUM_META_NAME = "_meta.json"


def load_album_meta(dir_path: Any) -> Dict[str, Any]:
    """读相册目录的 ``_meta.json``（mtime 缓存）；无/坏文件返回 {}。"""
    try:
        p = Path(str(dir_path or "")) / ALBUM_META_NAME
        if not p.is_file():
            return {}
        mt = p.stat().st_mtime
        key = str(p)
        cached = _ALBUM_META_CACHE.get(key)
        if cached and cached[0] == mt:
            return cached[1]
        import json as _json
        data = _json.loads(p.read_text(encoding="utf-8"))
        meta = data if isinstance(data, dict) else {}
        _ALBUM_META_CACHE[key] = (mt, meta)
        return meta
    except Exception:
        return {}


def album_file_meta(path: Any, meta: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """单文件的 {tod, scene, series}：sidecar 优先，文件名约定兜底；未知留空。"""
    name = Path(str(path or "")).name
    m = (meta or {}).get(name)
    m = m if isinstance(m, dict) else {}
    tod = str(m.get("tod") or "").strip().lower()
    scene = str(m.get("scene") or "").strip().lower()
    series = str(m.get("series") or "").strip()
    if not scene:
        scene = album_scene_class_of_path(name)
    if not series:
        series = album_series_of_path(name)
    return {"tod": tod, "scene": scene, "series": series}


def build_selfie_prompt(
    persona: Any,
    *,
    scene_hint: str = "",
    style: str = "",
    default_appearance: str = "",
    sfw: bool = True,
    content_rating: str = "",
    variety_salt: Optional[int] = None,
    lora_trigger: str = "",
    outfit: str = "",
) -> str:
    """构造形象照生成提示词（纯函数）。强制安全约束 + 强制人类单人主体。

    优先级：persona 真实外貌（appearance/visual/…）→ ``default_appearance``（config 可配）
    → 结构化字段推断（gender/age，见 ``_persona_smart_base``）→ 中性兜底。
    注意：任何分支都**不再**把人设原名（中文/非拉丁）写进 prompt——那对生图模型是噪声，
    还可能诱发画面里渲染文字。

    ``content_rating``（尺度分级，默认 ``sfw`` = 历史行为不变）：``suggestive`` 出「性感
    不露骨」照（擦边变现，仍硬保留不露点措辞 + image_gate 兜底拦露骨）；见
    ``normalize_content_rating``。空值时由旧参数 ``sfw`` 推（向后兼容）。

    ``variety_salt``（治「千篇一律」）：None=旧行为（恒「looking at the camera」，
    向后兼容、测试口径不变）；给了整数 → 经 ``selfie_variety`` 注入可轮换的取景/头部
    姿态/视线/表情/写实质感，让「同一个人」呈现不同自然瞬间。单人锚「solo, one person」
    始终保留（防跑偏）。

    ``lora_trigger``（角色 LoRA 部署）：非空则作**首个** token 前置（LoRA 靠训练时的
    稀有触发词唤起身份，须领衔）；空=不加（无 LoRA 时零影响，向后兼容）。

    ``outfit``（P1 今日衣着状态，2026-07-27）：非空则在场景后注入
    ``wearing <outfit>``——衣着从「模型自由发挥」变成确定性事实
    （``outfit_state.current_outfit``：同日恒定/连续窗跟随刚发照片），
    聊天状态块同源注入 → 图-文-跨链衣着一致。空=不注入（旧行为）。
    """
    base = (
        _persona_visual(persona)
        or str(default_appearance or "").strip()
        or _persona_smart_base(persona)
        or "a warm, friendly young woman, gentle expression"
    )
    v = selfie_variety(variety_salt) if variety_salt is not None else None
    parts = []
    trig = str(lora_trigger or "").strip()
    if trig:
        parts.append(trig)  # LoRA 触发词领衔（激活训练的身份）
    parts.append(f"Portrait selfie photo of {base}")
    # 反跑偏硬约束：单人、真人（FLUX cfg=1 无负面提示词，只能靠正面措辞钉死主体）。
    # 视线/景别/姿态/表情：有 variety_salt 时随池轮换（自然多样），否则恒「看镜头」。
    if v is not None:
        parts.append("solo, one person")
        parts.append(f"{v['framing']}, {v['head']}, {v['gaze']}, {v['expr']}")
    else:
        parts.append("solo, one person, looking at the camera")
    sc = str(scene_hint or "").strip()
    if sc:
        parts.append(sc)
    of = str(outfit or "").strip()
    if of:
        parts.append(f"wearing {of}")
    st = str(style or "").strip() or "natural lighting, candid, photorealistic, high quality"
    parts.append(st)
    if v is not None:
        parts.append(v["realism"])
    rating = normalize_content_rating(content_rating, sfw=sfw)
    if rating == "suggestive":
        # 成年锚：suggestive 档钉「成年」，与 image_gate 未成年硬红线双保险。
        # （sfw 档不加，保持历史 prompt 逐字不变。）
        parts.append("adult, mature")
    clause = _RATING_CLAUSES.get(rating)
    if clause:
        parts.append(clause)
    return ", ".join(p for p in parts if p)


def stable_selfie_seed(key: str, salt: int = 0) -> int:
    """按 persona 键派生确定性种子 ∈ [0, 2^31)：同一人设的自拍跨请求共享噪声基线，
    配合固定 appearance 让"同一个人"的观感更稳（彻底锁脸靠相册复用/img2img，见 autosend）。
    空键返回 -1（=随机种子）。

    ``salt``（治「构图千篇一律」）：默认 0=旧行为（同人设恒定底噪，向后兼容）；
    非 0 时把 salt 掺进派生 → 同人设不同 salt 得到不同底噪（构图/姿态随之变化），
    身份一致性交由 PuLID/角色 LoRA 保障。调用方传每次发送变化的 salt 即每张构图不同。
    """
    k = str(key or "").strip()
    if not k:
        return -1
    import zlib

    try:
        s = int(salt or 0)
    except Exception:
        s = 0
    payload = k if s == 0 else f"{k}#{s}"
    return zlib.crc32(payload.encode("utf-8")) % (2 ** 31)


def scene_pool(persona: Any, fallback_scenes: Any = None) -> list:
    """场景池解析（纯函数）：persona ``selfie_scenes`` 优先，回落 config 列表；无则空。"""
    if isinstance(persona, dict):
        raw = persona.get("selfie_scenes")
        if isinstance(raw, (list, tuple)) and raw:
            pool = [str(x).strip() for x in raw if str(x).strip()]
            if pool:
                return pool
    if isinstance(fallback_scenes, (list, tuple)):
        return [str(x).strip() for x in fallback_scenes if str(x).strip()]
    return []


# ── 场景-时段硬冲突过滤（2026-07-14 Phase19 时间一致性）────────────────────────
# 背景：bucket 只是轮换索引偏移、不做语义匹配 → 凌晨 3 点可能取到
# "campus walkway, afternoon light"（深夜发白天图，直接穿帮）。
# 词表刻意保守：只剔**硬冲突**（深夜 vs 白天词），模糊光线词（lamp light/
# window light 白天黑夜都成立）不剔——宁可放过，不误杀人设场景池。
_SCENE_DAY_WORDS = ("morning", "sunrise", "dawn", "noon", "midday", "afternoon",
                    "daytime", "daylight", "sunny day")
_SCENE_EVENING_WORDS = ("evening", "sunset", "dusk", "golden hour")
_SCENE_NIGHT_WORDS = ("night", "midnight", "late night")

# 深夜(22-6)的白天场所词（2026-08-22「凌晨 3:31 在二手书店看书」实录修复）：
# 负面剔除词表只认显式时间词 → "secondhand bookstore aisle" 这类无时间词场景
# 深夜照样入选并注进聊天「你此刻的状态」。深夜的语义是正面白名单（正常人只会
# 在室内/家里），故对下列公共场所词补一刀：命中且**无显式夜间词**才剔——
# "night street food market" 自带 night 放行；24h 场所语义（夜班/夜市/酒吧）
# 应在场景短语里显式带 night 词表达。词表保守：只收「深夜出现＝常识违背」。
_SCENE_DAYTIME_VENUE_WORDS = (
    "bookstore", "bookshop", "library", "campus", "classroom", "school",
    "university", "office", "meeting room", "coworking", "mall",
    "stationery", "metro", "subway", "commute", "market", "cafe",
    "coffee shop", "dessert", "bubble tea", "boba", "gym", "yoga studio",
    "pool", "convenience store", "shop counter",
)

# 深夜全池冲突时的中性回退场景（聊天注入与生图两个消费面都安全）。
NIGHT_NEUTRAL_SCENE = "at home, winding down for the night, cozy dim light"


def scene_conflicts_with_hour(scene: str, hour: int) -> bool:
    """场景短语的时间词与当前小时是否**硬冲突**（纯函数）。

    上午(6-11)剔正午/下午/黄昏/夜景；午后(11-17)剔清晨/夜景；
    傍晚(17-22)剔清晨/正午；深夜(22-6)剔全部白天+黄昏词，**且**剔无夜间词
    的白天场所（书店/图书馆/办公室/商场…——2026-08-22 实录补刀，见词表注释）。
    无时间词且非白天场所恒 False（中性场景任何时段可用）。

    2026-07-15 收紧：原白天段(6-17)只剔夜景 → 清晨 7:44 照样取到
    "campus walkway, afternoon light" 注入聊天，LLM 顺着场景说出
    「刚下课回来」——上午 vs 下午/黄昏同样是硬冲突，一并剔除。
    """
    s = str(scene or "").lower()
    if not s:
        return False
    h = int(hour)
    if 6 <= h < 11:
        return any(w in s for w in
                   ("noon", "midday", "afternoon")
                   + _SCENE_EVENING_WORDS + _SCENE_NIGHT_WORDS)
    if 11 <= h < 17:
        return any(w in s for w in
                   ("morning", "sunrise", "dawn") + _SCENE_NIGHT_WORDS)
    if 17 <= h < 22:
        return any(w in s for w in ("morning", "sunrise", "dawn", "noon", "midday"))
    if any(w in s for w in _SCENE_DAY_WORDS + _SCENE_EVENING_WORDS):
        return True
    if any(w in s for w in _SCENE_NIGHT_WORDS):
        return False
    return any(w in s for w in _SCENE_DAYTIME_VENUE_WORDS)


def pick_scene_hint(
    persona: Any,
    *,
    default_scene: str = "",
    fallback_scenes: Any = None,
    now: Any = None,
    salt: int = 0,
    weather_snap: Any = None,
) -> str:
    """场景轮换（纯函数）：按「日期 + 时段 + salt」从场景池确定性取一条。

    池优先级：persona ``selfie_scenes``（贴人设的场景，如学生=宿舍/便利店、
    金融顾问=办公室/高尔夫）→ config ``scene_rotation`` 列表 → ``default_scene``。
    同一天同一时段取值稳定（同 seed 下重复请求不出两张不同图）；``salt``（如相册
    已有张数）用于自动扩容时错开场景。时段：晨/午/晚/夜 4 档。

    2026-07-14（Phase19）：先按当前时段剔除**硬冲突**场景（凌晨不取
    "afternoon light"——聊天注入/生图都从这取，深夜发白天图直接穿帮）再轮换；
    白天段全池冲突则回退原池（有场景总比没场景强）。
    **深夜段（22-6）全池冲突不回退原池**（2026-08-22 修）：回退原池＝把刚剔掉的
    白天场所又请回来（「凌晨在二手书店」的再入口），改回退 ``NIGHT_NEUTRAL_SCENE``
    ——深夜「在家收尾」永远是安全事实，聊天与生图两个消费面同受益。
    ``weather_snap``（可选）：再滤高置信天气冲突（沙滩×暴雨）；全滤空则回退时段池。
    """
    pool = scene_pool(persona, fallback_scenes)
    if not pool:
        return str(default_scene or "").strip()
    import datetime as _dt

    t = now if isinstance(now, _dt.datetime) else _dt.datetime.now()
    h = t.hour
    fitting = [s for s in pool if not scene_conflicts_with_hour(s, h)]
    if fitting:
        pool = fitting
    elif h >= 22 or h < 6:
        return NIGHT_NEUTRAL_SCENE
    if weather_snap is not None:
        try:
            from src.companion.weather_state import scene_conflicts_with_weather
            weather_ok = [
                s for s in pool
                if not scene_conflicts_with_weather(s, weather_snap)
            ]
            if weather_ok:
                pool = weather_ok
        except Exception:
            pass
    bucket = 0 if 6 <= h < 11 else 1 if 11 <= h < 17 else 2 if 17 <= h < 22 else 3
    idx = (t.timetuple().tm_yday * 4 + bucket + int(salt)) % len(pool)
    return pool[idx]


_TIME_PHRASE_BY_HOUR = (
    ((5, 8), "early morning soft light"),
    ((8, 11), "morning light"),
    ((11, 14), "midday light"),
    ((14, 17), "afternoon light"),
    ((17, 20), "evening, golden hour light"),
    ((20, 23), "night, warm indoor light"),
)
_LATE_NIGHT_PHRASE = "late night, dim cozy indoor light"
_ANY_TIME_WORDS = (_SCENE_DAY_WORDS + _SCENE_EVENING_WORDS + _SCENE_NIGHT_WORDS)


def ensure_time_of_day(scene: str, now: Any = None, weather_snap: Any = None) -> str:
    """给**没有时间词**的场景短语补当前时段光线氛围（纯函数，Phase19）。

    用途：LLM 发图指令（photo_directive）的场景直通生图 prompt——LLM 通常会带
    时间（协议要求），漏了就按服务器当前时间兜底，保证凌晨要图不出正午烈日照。
    已有任何时间词 → 原样返回（尊重 LLM 的对话内理解，它可能在描述"上次白天拍的"）。
    空场景返回空（交上游轮换池路径，那边已有时段过滤）。

    ``weather_snap``（2026-08-18 天气匹配生活照）：强天气桶（雨/雪/雾/雷暴）时
    追加氛围短语——下雨天发的照片带雨天的光，与文案里的真实天气同一个世界；
    晴/多云/无快照零改变。时间词短路**不跳过**天气后缀（「上次白天拍的」是
    时间语义，天气仍是此刻的）——但场景自带天气词时后缀让行（见
    ``weather_scene_suffix``）。
    """
    sc = str(scene or "").strip()
    if not sc:
        return sc
    _wx = ""
    if weather_snap is not None:
        try:
            from src.companion.weather_state import weather_scene_suffix
            _wx = weather_scene_suffix(sc, weather_snap)
        except Exception:
            _wx = ""
    low = sc.lower()
    if any(w in low for w in _ANY_TIME_WORDS):
        return sc + (", " + _wx if _wx else "")
    import datetime as _dt
    t = now if isinstance(now, _dt.datetime) else _dt.datetime.now()
    h = t.hour
    phrase = _LATE_NIGHT_PHRASE
    for (lo, hi), p in _TIME_PHRASE_BY_HOUR:
        if lo <= h < hi:
            phrase = p
            break
    return sc + ", " + phrase + (", " + _wx if _wx else "")


def build_scene_choice_instruction(
    directive: str, facts: Any, scenes: list,
) -> str:
    """「场景反选」LLM 指令（纯函数，Phase18）：按主动话题从场景池选最贴合的一条。

    例：回访"备考 N1" → 选"宿舍书桌/图书馆"而非"夜市"。只输出编号；0=没有明显贴合
    （调用方回落时段轮换）。解析见 ``parse_scene_choice``。
    """
    lines = [f"{i + 1}. {s}" for i, s in enumerate(scenes)]
    fs = [str(f).strip() for f in (facts or []) if str(f).strip()][:3]
    fact_block = ("；相关背景：" + "；".join(fs)) if fs else ""
    return (
        "你在为一条聊天消息选配图场景。消息话题：「"
        + str(directive or "").strip()[:160] + "」" + fact_block + "\n"
        "候选场景（英文描述）：\n" + "\n".join(lines) + "\n"
        "选出与话题最贴合的场景编号；如果没有明显贴合的，回答 0。"
        "只输出一个数字，不要任何其它内容。"
    )


def parse_scene_choice(reply: Any, n: int) -> int:
    """解析场景反选回答 → 编号 ∈ [0, n]；解析不出/越界返回 -1（调用方回落轮换）。"""
    import re

    m = re.search(r"-?\d+", str(reply or ""))
    if not m:
        return -1
    try:
        v = int(m.group(0))
    except Exception:
        return -1
    return v if 0 <= v <= int(n) else -1


def resolve_current_scene(
    persona: Any,
    scfg: Dict[str, Any],
    *,
    now: Any = None,
    salt: int = 0,
    weather_snap: Any = None,
) -> str:
    """「AI 此刻在哪/在干嘛」的**单一事实源**（纯函数，Phase18 场景状态化）。

    背景（图文打脸事故面）：聊天文本 LLM 自由发挥（"我在上班"），生图链独立按
    日期轮换场景（出"海边落日"）——两者都是果，没有共同的因。本函数把
    ``pick_scene_hint`` 的确定性轮换（同日同时段恒定）升格为会话级「当前场景状态」：
    聊天 prompt（``scene_chat_note``）与生图 prompt（Stage A / autosend / proactive）
    都从这里取，图文天然同源，零 LLM 调用、零提取误判。

    取值口径与既有生图链完全一致（persona ``selfie_scenes`` → config
    ``scene_rotation`` → ``scene_hint``），保证收口不改变已有出图行为。
    ``weather_snap`` 可选：滤高置信天气冲突场景。
    """
    return pick_scene_hint(
        persona,
        default_scene=str((scfg or {}).get("scene_hint") or ""),
        fallback_scenes=(scfg or {}).get("scene_rotation"),
        now=now,
        salt=salt,
        weather_snap=weather_snap,
    )


# 行程线四时段（代表小时须与 pick_scene_hint 的 bucket 划分一一对应：
# 6-11 上午 / 11-17 下午 / 17-22 傍晚 / 22-6 深夜）——同一确定性函数按各桶取值，
# 「当前真实时刻」与「行程线当前桶」必然同场景（同 bucket+yday+salt）。
_ITINERARY_BUCKETS = ((8, "上午"), (13, "下午"), (19, "傍晚"), (23, "深夜"))


def _bucket_label(hour: int) -> str:
    h = int(hour)
    if 6 <= h < 11:
        return "上午"
    if 11 <= h < 17:
        return "下午"
    if 17 <= h < 22:
        return "傍晚"
    return "深夜"


def build_day_itinerary(
    persona: Any, scfg: Dict[str, Any], *, now: Any = None,
    weather_snap: Any = None,
) -> list:
    """「今天的动线」（纯函数，Phase20 时间叙事）：把场景状态从「点」升级为「线」。

    同一个确定性轮换（``resolve_current_scene``）按今天四个时段各取一条 →
    ``[(时段名, 场景), ...]``——聊天注入后 LLM 可自然引用「早上去过哪/晚点打算
    干嘛」（过去/将来时态），跨时段叙事连贯；每桶各自经 Phase19 时段冲突过滤
    （深夜桶不会排到 afternoon 场景）。零 LLM、零存储：明天动线自动换（yday 变）。
    场景池空 → []（调用方不渲染行程线）。
    """
    import datetime as _dt
    t = now if isinstance(now, _dt.datetime) else _dt.datetime.now()
    out = []
    for hour, label in _ITINERARY_BUCKETS:
        try:
            rep = t.replace(hour=hour, minute=0, second=0, microsecond=0)
            sc = resolve_current_scene(
                persona, scfg, now=rep, weather_snap=weather_snap)
        except Exception:
            sc = ""
        if sc:
            out.append((label, sc))
    return out


def scene_chat_note(
    scene: str, itinerary: Any = None, *, now: Any = None, outfit: str = "",
) -> str:
    """把场景短语渲染成聊天 prompt 的「状态设定」块（纯函数）。

    措辞要点：内部设定而非播报指令——只在对方问起/话题相关时自然引用，
    防 LLM 每条都汇报位置（那比不一致更机器人）。场景池是英文短语（生图用），
    LLM 需换成口语转述而非原样输出。
    ``itinerary``（Phase20 可选）＝``build_day_itinerary`` 输出：注入「今天动线」
    一行，LLM 获得跨时段叙事（早上做过什么/晚点打算什么），当前时段以（现在）标注。
    ``outfit``（P1 今日衣着状态）＝``outfit_state.current_outfit`` 输出：注入
    「今天穿着」事实行——被问「穿了什么」按此答，与生图 prompt 同源不再现编。
    """
    sc = str(scene or "").strip()
    if not sc:
        return ""
    lines = [
        "【你此刻的状态（内部设定）】你现在的场景：" + sc + "。",
    ]
    try:
        from src.companion.outfit_state import outfit_chat_line
        _ol = outfit_chat_line(outfit)
        if _ol:
            lines.append(_ol)
    except Exception:
        pass
    rows = [r for r in (itinerary or [])
            if isinstance(r, (list, tuple)) and len(r) == 2 and str(r[1]).strip()]
    if len(rows) >= 2:
        import datetime as _dt
        t = now if isinstance(now, _dt.datetime) else _dt.datetime.now()
        cur = _bucket_label(t.hour)
        seq = "→".join(
            f"{label}{'(现在)' if label == cur else ''}:{str(s).strip()}"
            for label, s in rows)
        lines.append(
            "你今天的动线：" + seq + "。可自然提到今天早些时候做过的事"
            "（过去时）或晚点的打算（将来时），前后保持一致。")
    lines.append(
        "仅当对方问你在哪/在干嘛、或话题自然相关时，用口语顺带提到（翻译成对话语言，"
        "不要输出英文原文、不要每条都汇报位置、不要与它矛盾——比如场景在家就别说自己在公司加班）。")
    return "\n".join(lines)


# ── 饮食状态事实源（2026-07-15「还没吃→刚吃了面包」矛盾事故修复）───────────────
# 背景：「吃没吃饭」这类临时自身状态此前没有事实源，全靠 LLM 每次现编——两次生成
# 自然打架。这里按 persona+日期确定性生成今天三餐的「进餐时刻」，任何时刻查询结论
# 恒定（同一天内多次生成读到同一份事实），与场景状态（resolve_current_scene）同哲学：
# 零 LLM、零存储、明天自动换。时刻在合理窗口内按 hash 抖动，不同人设/不同日期各异。
_MEAL_SPECS = (
    # (餐名, 窗口起, 窗口止)——进餐时刻在窗口内确定性抖动
    ("早饭", 7.0, 9.0),
    ("午饭", 11.5, 13.5),
    ("晚饭", 17.5, 19.5),
)
_MEAL_RECENT_HOURS = 1.5   # 进餐后 1.5h 内算「刚吃过」


def resolve_meal_state(persona_id: str, now: Any = None) -> str:
    """「此刻吃没吃饭」的单一事实源（纯函数）：返回一句中文事实描述，空串=不注入。

    规则：按 persona+日期 hash 出今天三餐时刻 → 与当前时间比对：
      - 落在某餐时刻后 1.5h 内 → 「刚吃过X饭」
      - 落在某餐窗口内但还没到该餐时刻 → 「还没吃X饭（快到饭点了）」
      - 其他时段 → 「上一顿是X饭，已经吃过了」/ 清晨 → 「今天还没吃东西」
    同一天内任意两次调用结论一致——防重复系统换角度重写时事实不再漂移。
    """
    import datetime as _dt
    import zlib as _zlib

    t = now if isinstance(now, _dt.datetime) else _dt.datetime.now()
    day = t.strftime("%Y%m%d")
    cur = t.hour + t.minute / 60.0
    meals = []   # [(餐名, 进餐时刻小时数), ...]
    for name, lo, hi in _MEAL_SPECS:
        seed = _zlib.crc32(f"{persona_id}:{day}:{name}".encode("utf-8"))
        at = lo + (seed % 1000) / 1000.0 * (hi - lo)
        meals.append((name, at))

    # 刚吃过某餐（1.5h 内）
    for name, at in meals:
        if at <= cur < at + _MEAL_RECENT_HOURS:
            return f"你刚吃过{name}不久。"
    # 处于某餐窗口、但确定性进餐时刻还没到 → 还没吃这顿
    for (name, lo, hi), (_n, at) in zip(_MEAL_SPECS, meals):
        if lo - 0.5 <= cur < at:
            return f"你还没吃{name}（快到饭点了，有点饿）。"
    # 不在任何饭点附近：报最近一顿已吃
    eaten = [(name, at) for name, at in meals if cur >= at + _MEAL_RECENT_HOURS]
    if eaten:
        return f"你今天的{eaten[-1][0]}已经吃过了，现在不在饭点。"
    if cur < 6.0:
        return "现在是深夜，不在饭点（要吃也只是夜宵）。"
    return "你今天还什么都没吃（刚起床不久）。"


def meal_state_note(persona_id: str, now: Any = None) -> str:
    """把饮食状态渲染成 prompt 事实块（纯函数）。空 persona_id 也可用（按空串 hash）。"""
    fact = resolve_meal_state(persona_id, now=now)
    if not fact:
        return ""
    return (
        "【你的饮食状态（内部事实）】" + fact
        + "被问到吃了没/聊到吃饭话题时必须与此一致，不要主动汇报；"
          "换措辞可以，事实不能变。"
    )


# 用户显式点名的拍摄场景（"发张你在海边的照片"）→ 生图 directive.scene 覆盖轮换。
# 保守词表（宁缺勿滥）：只收高置信、名词性、可安全出图的场景；衣着/姿态类不收（易滑向 NSFW 请求，
# 交由 SFW 硬约束与正常轮换处理）。value 为英文生图短语（与 scene_rotation 池同格式）。
_REQUESTED_SCENE_MAP: tuple = (
    (("海边", "海邊", "沙滩", "沙灘", "beach", "seaside"),
     "at the beach, sea in the background"),
    (("咖啡厅", "咖啡廳", "咖啡店", "cafe", "coffee shop"),
     "in a cozy cafe, holding a coffee cup"),
    (("办公室", "辦公室", "上班", "公司", "office"),
     "at the office desk, workday casual"),
    (("健身房", "健身", "gym", "workout"),
     "at the gym in sporty outfit, post-workout glow"),
    (("厨房", "廚房", "做饭", "做菜", "煮饭", "kitchen", "cooking"),
     "in the kitchen cooking, apron on"),
    (("公园", "公園", "户外", "戶外", "park", "outdoors"),
     "outdoors in a park, natural daylight"),
    # Q-6 E（CK8HCT）：「风景/landscape/scenery」此前不在表 → 索风景走通用自拍池。
    (("风景", "風景", "风景照", "風景照", "landscape", "scenery", "scenic"),
     "outdoors scenic landscape view"),
    (("家里", "家裡", "在家", "沙发", "沙發", "at home", "on the couch"),
     "at home on the couch, cozy and relaxed"),
    (("卧室", "臥室", "床上", "bedroom"),
     "in the bedroom, soft warm light, fully clothed casual"),
    (("图书馆", "圖書館", "书店", "書店", "library", "bookstore"),
     "in a quiet library with bookshelves"),
    (("街上", "街头", "街頭", "逛街", "street", "shopping"),
     "on a city street, casual street style"),
    # 实施69：夜市/街边摊点名（实录：人设「烧烤店老板娘」自称在夜市摊，客户要
    # 图全程无法定向；归一层 scene_class_of 早认识「夜市→street」，这张表却
    # 不认——一份知识两张表的漂移，本条对齐）。短语含 night market 关键词 →
    # scene_class_of 归一仍是 street，相册 street_* 存货可命中。
    (("夜市", "大排档", "大排檔", "街边摊", "街邊攤", "路边摊", "路邊攤",
      "night market", "food stall"),
     "at a night market street food stall, street lamps glowing"),
    (("雨", "下雨", "rain", "rainy"),
     "by the window on a rainy day"),
    (("夜景", "晚上外面", "night view"),
     "city night lights in the background"),
)


def extract_requested_scene(text: str) -> str:
    """从客户要图消息里提取**显式点名的场景**（纯函数；没点名返回空串走正常轮换）。

    只在文本本身是要图请求时才有意义（调用方先过 detect_selfie_request /
    offer-accept 桥）；这里只做词表映射，刻意不用 LLM（要图路径延迟敏感）。
    """
    t = str(text or "").strip().lower()
    if not t or len(t) > 200:
        return ""
    for keys, scene in _REQUESTED_SCENE_MAP:
        for k in keys:
            if k in t:
                return scene
    return ""


# ── 叙事自称场景（实施69 P2，2026-08-27）────────────────────────────────────
# AI 上一轮亲口说「我在夜市摊这儿呢」，本轮客户泛化要图 → 选图应贴**嘴上说过
# 的地方**而非轮换值（实录 23:03 说在夜市、23:06 发居家自拍=第一穿帮点；scene
# 轮换与 LLM 顺人设即兴的叙事本就可能分叉，图跟叙事才是客户视角的一致）。
# 窄口径：必须是「我+（副词）+在/到/来到/搁+片段」的自述结构，片段能归
# canonical 场景类才算（「我在想你」「你猜我在哪」都不命中）；「我不在家」
# 因「不」隔断不命中。消费方＝Stage A prefer_scene（软偏好，无货不拒发）。
_SELF_SCENE_RE = re.compile(
    r"(?:我|人家|咱)\s*(?:现在|現在|这会儿|這會兒|正|刚|剛|就)?\s*"
    r"(?:在|到了?|来到|來到|搁|擱)\s*([^\s，。！？,!?~～\n]{1,10})")


def extract_self_claimed_scene(text: str) -> str:
    """AI 出站文本里「我在X」自称场景 → canonical 场景类；认不出返回 ''。"""
    s = str(text or "")
    if not s.strip() or len(s) > 400:
        return ""
    try:
        from src.companion.persona_media import scene_class_of
    except Exception:
        return ""
    for m in _SELF_SCENE_RE.finditer(s):
        cls = scene_class_of(m.group(1))
        if cls:
            return cls
    return ""


# 「跟上次一样的」指涉（要图请求里点名复刻上次场景）→ 调用方从已发媒体日志取上次 scene。
_SAME_SCENE_MARKERS = (
    "跟上次一样", "和上次一样", "同上次", "上次那样", "上次那种", "像上次",
    "跟上次一樣", "和上次一樣", "上次那樣", "上次那種",
    "same as last", "like last time", "like the last one",
)


def wants_same_scene(text: str) -> bool:
    """客户是否在要「跟上次一样场景」的照片（纯函数；场景本身由调用方查日志）。"""
    t = str(text or "").strip().lower()
    if not t or len(t) > 200:
        return False
    return any(m in t for m in _SAME_SCENE_MARKERS)


# 收图后的「质疑」信号词表（P0 观测补盲）：只收高置信表达；调用方须先确认
# 「最近真发过媒体」再判（没发过图时这些词多半在聊别的，不计入）。
_MEDIA_COMPLAINT_MAP: tuple = (
    ("repeat", (
        "又是这张", "又是這張", "又发这张", "又發這張", "同一张", "同一張",
        "一样的照片", "一樣的照片", "发过了", "發過了", "重复的图", "重複的圖",
        "上次发过", "上次發過", "same photo", "same pic", "already sent",
        "sent this before", "same picture again",
    )),
    ("not_you", (
        "不是你吧", "不像你", "根本不是你", "这是你吗", "這是你嗎", "换人了",
        "換人了", "别人的照片", "別人的照片", "长得不一样", "長得不一樣",
        "怎么变了个人", "doesn't look like you", "not you", "who is this",
        "looks like someone else",
    )),
    ("fake", (
        "照骗", "照騙", "网图", "網圖", "p的吧", "p图", "p圖", "ai画", "ai畫",
        "ai生成", "ai做的", "假的吧", "假照片", "骗人", "騙人", "fake",
        "photoshopped", "ai generated", "ai-generated", "not real",
    )),
    # 实施69（2026-08-27）：内容不符——照片到了但画面与叙事对不上（「说好的
    # 路灯/夜市在图里看不到」）。与 lie_caught（没收到）分开：两者的正确回应
    # 完全不同（前者=角度没拍到轻松带过；后者=别编传输借口）；实录 23:07 正是
    # 把内容质疑听成没收到才滚出「信号不好」连环谎。放 lie_caught 之前（特异
    # 词先判）；调用方按 repeat 族同口径设「24h 真发过媒体」采信闸。
    ("content_mismatch", (
        "照片里没", "照片裡沒", "图里没", "圖裡沒", "照片上没", "照片上沒",
        "图上没", "圖上沒", "画面里没", "畫面裡沒", "背景不对", "背景不對",
        "背景不像", "背景呢", "跟你说的不一样", "跟你說的不一樣",
        "和你说的不一样", "和你說的不一樣", "不是说在", "不是說在",
        "哪有你说的", "哪有你說的", "not in the photo", "don't see it in",
        "doesn't show", "photo doesn't match",
    )),
    # 实施69：催兑现——「照片呢/图呢」是等图未到的追讨，不是抓包指控。单独
    # 成类：①观测上与真抓包分开（催收量=承诺兑现链的健康度直读）②纠偏走
    # 悬置常驻 hint（带主体+催促升级），不占「止损认错」语气。原在 lie_caught
    # 的这几个词移入本类。
    ("unfulfilled", (
        "照片呢", "图呢", "圖呢", "语音呢", "語音呢", "说好的照片", "說好的照片",
        "怎么还没发", "怎麼還沒發", "怎么还没拍", "怎麼還沒拍", "还没拍好",
        "還沒拍好", "where's the photo", "where is the photo", "still waiting",
    )),
    # 2026-07-29 对练实证补漏：抓包「说发没发/说话不算话/冒充」——这类质疑此前
    # 全漏（四句里只「骗人」命中），是「连环解释」的触发前提没被识别的根因。
    ("lie_caught", (
        "没收到", "沒收到", "没看到", "沒看到", "没有收到", "沒有收到",
        "啥都没有", "什么都没有", "甚麼都沒", "没图", "沒圖",
        "你不是说", "你不是說",
        "对不上", "對不上", "时间线", "時間線", "前后矛盾",
        "前後矛盾", "打字算", "打的字算", "假唱", "冒充", "說話不算",
        "说话不算", "食言", "光说不做", "光說不做", "说了不做", "說了不做",
        "didn't get", "didnt get", "didn't receive", "nothing here",
        "you said you", "thought you said",
        # #259 P-3（6SRA2B 14:51「Where honey? No picture」未识别）复诉词补全：
        # 「没图」的各种口语形态——第二次复诉没被认出，AI 才有机会编第二句谎。
        "no picture", "no pic", "no photo", "no image", "nothing came",
        "nothing arrived", "still nothing", "never got", "never received",
        "don't see it", "dont see it", "don't see any", "dont see any",
        "didn't come through", "didnt come through",
        "where honey", "where babe", "where love",
        # Q-6 C（#263）：复诉词再补口语（P-3 已有 no picture / nothing came / still nothing /
        # 没收到；这里补 didn't get it / 没收到图，避免「图呢」之外的短句漏识别）。
        "didn't get it", "didnt get it", "didn't get the", "didnt get the",
        "you didn't send", "u didn't send", "you didnt send",
        "没收到图", "沒收到圖", "没收到照片啊", "沒收到照片啊",
        "没有收到", "没收到啊", "沒收到啊", "没有照片", "沒有照片", "没有图", "沒有圖",
        "图在哪", "圖在哪", "照片在哪", "什么都没收到", "什麼都沒收到", "啥也没",
        "啥都没", "还是没有", "還是沒有", "还是没收到", "還是沒收到",
    )),
    # 失望/敷衍/整体不信任（T10 型）——单独一类，配「停止解释、简短真诚」纠偏。
    ("distrust", (
        "敷衍", "失望", "算了吧", "都是假", "全是假", "没一句真", "沒一句真",
        "从头到尾", "從頭到尾", "一直在骗", "一直在騙", "没意思", "沒意思",
        "懒得理", "懶得理", "you're lying", "all fake", "so disappointed",
        "waste of time",
    )),
)

# 「解释/道歉螺旋」标记——AI 自己上一轮已在长篇解释/道歉/找借口时命中。
# 用于纠偏 hint 升级（别再解释、简短带过），防「被你抓到了→时间线圆场→更假」。
_APOLOGY_SPIRAL_MARKERS = (
    "被你抓到", "被你发现", "被你發現", "被抓包", "抓现行", "抓現行",
    "不好意思", "对不起", "對不起", "抱歉", "我错了", "我錯了", "我承认",
    "我承認", "心虚", "心虛", "老实交代", "老實交代", "其实我", "其實我",
    "时间线", "時間線", "解释一下", "解釋一下", "误会", "誤會", "你赢啦",
    "你贏啦", "为难我", "為難我", "忙忘", "忘了", "打断", "打斷",
    "sorry", "my bad", "i admit",
)


def detect_apology_spiral(text: str) -> bool:
    """上一轮 AI 回复是否已在道歉/解释/找借口（纯函数，供纠偏 hint 升级）。

    标记**总出现次数** ≥2（含同一标记重复，「我错了我错了」即螺旋）或 单条超长
    (>60字)且含 ≥1 标记 = 判定螺旋——单个「抱歉」是正常礼貌，连环解释才是问题。
    """
    t = str(text or "").lower()
    if not t.strip():
        return False
    hits = sum(t.count(m) for m in _APOLOGY_SPIRAL_MARKERS)
    return hits >= 2 or (hits >= 1 and len(t) > 60)


# #259 P-3：lie_caught **弱词**——「Where?」「哪呢」「没有啊」这类单看会误伤
# （「where are you from?」不含「where?」但「你在哪呢」会含「哪呢」），只在调用方
# 确认**媒体悬置中**（AI 承诺 / 真发过一张、客户在等图）时才采信。
_LIE_CAUGHT_WEAK_KEYS: tuple = (
    "where?", "where??", "where ?", "where is it", "where's it", "nothing here",
    "哪呢", "哪儿呢", "哪裡呢", "没有啊", "沒有啊", "没有呀", "沒有呀",
    "没啊", "沒啊", "没呢", "沒呢",
)


def detect_media_complaint(text: str, *, media_pending: bool = False) -> str:
    """客户是否在**质疑刚收到的图/抓包说谎**（纯函数）：返回 ``repeat``（图重复）/
    ``not_you``（不像本人）/``fake``（假图/网图）/``content_mismatch``（画面与叙事
    对不上，实施69）/``unfulfilled``（催兑现「照片呢」，实施69）/``lie_caught``
    （没收到/说话不算话/冒充）/``distrust``（整体失望敷衍）/``""``（无质疑）。

    图文质疑（repeat/not_you/fake/content_mismatch）是图文一致性劣化的第一现场
    信号；lie_caught/distrust 是「说发没发/连环解释」的触发前提（2026-07-29 对练
    补漏）。词表保守（宁漏勿滥）；repeat/not_you/fake/content_mismatch 调用方须先
    确认「最近真发过媒体」再采信，lie_caught/distrust 不强制该闸（没收到/唱歌
    冒充本就没有媒体台账）；unfulfilled 只计数不设纠偏 hint（措辞由悬置常驻
    hint 负责，见 skill_manager._maybe_flag_media_complaint）。

    ``media_pending=True``（#259 P-3）＝调用方已确认客户在等一张图（AI 刚承诺 /
    刚真发过）：此时「Where?」「哪呢」「没有啊」这类弱词也算 ``lie_caught``
    （6SRA2B 14:51「Where honey? No picture」第二次复诉此前完全没被认出）。
    """
    t = str(text or "").strip().lower()
    if not t or len(t) > 200:
        return ""
    for kind, keys in _MEDIA_COMPLAINT_MAP:
        for k in keys:
            if k in t:
                return kind
    if media_pending and len(t) <= 60:
        for k in _LIE_CAUGHT_WEAK_KEYS:
            if k in t:
                return "lie_caught"
    return ""


_STAGE_TEXTS: dict = {
    # Stage 短路直发文案（绕过 LLM 与出站翻译）——硬编码单语会对外语客户穿帮
    # （英文会话突然蹦中文）。zh/en 双语按会话语言取；其余语种暂用 en（比 zh 更
    # 通用），后续可接翻译引擎。
    # 2026-07-15 复盘两条硬规则：
    #   ① 全部**第一人称**——旧模板用 {name} 自称（"林小雨现在不太方便拍照"），
    #     正常人聊天不会用第三人称叫自己的名字，直接穿帮；
    #   ② 高频兜底键（no_photo/capped）为**变体池**——发图连续失败时同一句台词
    #     原样复读（11:24 与 12:07 两次一字不差）比婉拒本身更机器人。
    "too_soon": {
        "zh": "哎呀，我们才刚开始熟悉呢，等再多聊聊、更亲近一点，就给你看我的样子好不好～",
        "en": "hehe, we just started getting to know each other~ chat with me a bit more and I'll show you what I look like 😊",
    },
    "capped": {
        "zh": [
            "今天已经拍了好多照片啦，有点累咯～明天再给你拍新的好不好？😊",
            "呜，今天拍太多啦，脸都笑僵了…明天第一张就拍给你嘛～",
            "今天的拍照额度被我用完啦哈哈，明天补你一张新鲜的～",
        ],
        "en": [
            "I've taken so many photos today, a little tired now~ I'll take a new one for you tomorrow, okay? 😊",
            "haha my face is sore from posing all day~ first pic tomorrow goes to you, promise 😊",
        ],
    },
    "no_photo": {
        "zh": [
            "这会儿不太方便拍照呢…回头拍了马上发你！先多陪我聊会儿嘛～",
            "唔，现在拍不了呢，等下补给你好不好？先跟我说说你那边怎么样啦",
            "手机这会儿不太给力，拍不出来…晚点偷偷拍一张发你😝",
            "现在不方便拍呀，回头一定补上！你刚说的那个我还想听呢～",
        ],
        "en": [
            "I can't really take a photo right now~ but I'm right here with you. Talk to me a bit more, okay? 😊",
            "can't take one right this second~ I'll sneak one for you later, promise 😝",
            "my phone's being weird right now... I'll make it up to you with a photo later, okay?",
        ],
    },
    "caption": {
        # 2026-07-22 复盘（whatsapp 阿龙会话实录）：一分钟内连发 3 张相册图，
        # 配文一字不差都是「这是刚拍的」→ 客户当场质疑"你刚拍的怎么衣服和场景
        # 都变了""脸怎么这么假"。两条教训：① 高频配文键也必须是变体池（同
        # no_photo/capped 的防复读语义）；② album 后端发的是**旧照**，配文不能
        # 每次都声称「刚拍」——变体池里只保留一条弱时间声明（「刚拍」≤1，有门禁），
        # 图文一致性（media_consistency_eval「附图否认/无图称已发」红线）不受
        # 影响：这些句子都只随真实已附图的消息发出。
        # 2026-08-03 复读治理扩容：生产 7 天出站抓到本池首句 ×4 逐字复读 →
        # zh ≥10 / en ≥6 + 按会话避重轮换（pick_caption）。本池是**新照口径**
        # （随刚生成的图发出），允许现在时措辞。
        "zh": [
            "给你看张照片～喜欢吗？😊",
            "喏，照片来啦～要给好评哦😆",
            "偷偷发你一张，不许外传哦😝",
            "翻了张我觉得好看的发你，嘿嘿～",
            "这张怎么样？拍的时候光线刚刚好😊",
            "刚拍的哦，快夸我😝",
            "拍了张新的，第一个发给你😊",
            "来～今日份的我，请查收😆",
            "咔嚓一张，你要的照片来咯～",
            "看看这张，我觉得拍得还挺好的😊",
            "给你发张照片，看看最近的我～",
            "拍了一张感觉还不错的，给你看看嘛",
        ],
        "en": [
            "here's a pic for you~ do you like it? 😊",
            "sending you one, don't share it around 😝",
            "picked one I really like, just for you 😊",
            "how's this one? the lighting was so nice~",
            "took a new one for you, be nice 😆",
            "one more, for your eyes only 😊",
            "say hi to today's me~ 😆",
        ],
    },
    "caption_object": {
        # 物体图（对话里提到的东西）配文——新照口径（图是现场生成的）。
        # 2026-08-03 复读治理扩容：zh ≥10 / en ≥6。
        "zh": [
            "拍好啦，给你看～😊",
            "喏，就是这个～",
            "看，拍给你啦😆",
            "拍了一张，怎么样？",
            "给你瞧瞧，就是它😊",
            "专门拍给你看的哦～",
            "照片来啦，就是这个样子😆",
            "喏喏，拍到了，给你～",
            "就是这个啦，好看吧😊",
            "拍了发你，快看看～",
        ],
        "en": [
            "here you go, just took it~ 😊",
            "there it is~ 😊",
            "took this for you, see? 😆",
            "snapped it for you~ 😊",
            "here's what it looks like 😆",
            "see? told you I'd show you~",
        ],
    },
    "caption_album": {
        # P0 一致性（2026-07-27）：**明确旧照口径**的配文池——注册相册/相册兜底
        # 发的是存货，配文声称「刚拍」曾被客户按衣着/时间对不上当场戳穿。
        # 本池所有变体都不含"刚拍/现在"类时间声明，诚实且自然。
        # 2026-08-03 复读治理扩容：生产 7 天出站抓到本池前三句 ×10/×4/×3 逐字
        # 复读（进程游标重启归零恒取池首 + 池太小）→ zh ≥10 / en ≥6 + 按会话
        # 避重轮换（pick_caption）。**红线不变**：全部「翻出来的/之前拍的/存货」
        # 口径，禁「刚拍/刚出炉/热乎」等新鲜词（tests/test_persona_media_consistency
        # + tests/test_caption_variety 双门禁扫池）。
        "zh": [
            "翻到一张之前拍的，给你看～😊",
            "找到一张我还挺喜欢的，分享给你嘿嘿",
            "这张是前阵子拍的，感觉还不错吧～",
            "给你看张我的照片，不许笑话我哦😝",
            "翻相册翻到这张，越看越顺眼，给你也看看～",
            "存货里挑了张顺眼的，只给你看哦😊",
            "之前拍的这张一直没舍得发，便宜你啦😝",
            "相册里躺了好久的一张，想起来给你看看",
            "这张有点日子了，不过我真的挺喜欢～",
            "挖出一张旧照，你猜是什么时候拍的？",
            "手机里存的这张，感觉状态还行，给你瞧瞧😊",
            "之前随手拍的一张，居然还挺好看的嘿嘿",
        ],
        "en": [
            "found an older pic of me, here you go~ 😊",
            "this one's from a while back, but I kinda like it hehe",
            "sharing one of my favorite pics with you 😊",
            "dug this one up from my gallery, what do you think?",
            "an old one I never got around to sending~ 😝",
            "was scrolling through my album and thought of you, so here 😊",
            "this one's been sitting in my phone for a while, still cute right?",
        ],
    },
    "promise_fail": {
        # 异步兑现失败的补偿（承诺文本已发出、图没出来）：像真人一样找个台阶，
        # 不否认能力、给出「改天补」的软承诺（远期承诺不在守卫打击面）。
        "zh": "呜呜刚拍好想发给你，手机突然抽风传不上去…改天一定补给你，别嫌弃我嘛😢",
        "en": "ugh I took one but my phone is acting up and it won't upload... I'll make it up to you another day, promise 😢",
    },
    "upsell_lead": {
        "zh": "我的照片是只给最亲近的人看的小秘密哦～",
        "en": "my photos are a little secret I only share with someone really close~ ",
    },
    "upsell_fallback": {
        "zh": "解锁「专属相册」就能看到我的照片啦～",
        "en": "unlock my private album and you'll get to see me~ 😊",
    },
}

# 变体池轮换游标（进程级）：连续两次触发同一 key 必换措辞——防复读的兜底保证。
# 2026-08-03 复读治理：游标**起点随机**（旧实现恒从池首起步——生产机重启频繁，
# 每个进程生命周期的首次触发都取池[0]，7 天出站「翻到一张之前拍的给你看」×10
# 的直接根因）；起步后仍逐次 +1（「连续必换」既有保证不变；显式 variant_salt
# 时完全确定性，测试口径不变）。带 chat_key 的消费点优先走 pick_caption
# （按会话×日期确定性 + 近用避重，见下）。
_STAGE_VARIANT_SEQ: dict = {}

# ── 按会话避重的配文轮换（2026-08-03 发图配文复读治理）─────────────────────────
# 生产实锤：近 7 天出站发图配文高度模板化（「翻到一张之前拍的给你看」×10 等），
# 同一客户几天内反复收到逐字相同的配文＝一眼机器人。固定配文池是 LLM 防复读
# 守卫（reply_variety）管不到的静态文案层，治理在选取层做两件事：
#   ① 种子＝crc32(chat_key + 日期)——不同会话同一天各取不同条、同会话同日
#      （无新记账时）恒定可测；
#   ② 近用避重账本（进程级 bounded：512 会话 × 最近 3 条配文指纹）——真发出
#      后由消费方调 note_caption_used 记账；候选命中近用则顺移下一条，全池
#      都近用过按种子取（绝不死循环/拒答）。账本重启即清（可接受：种子层已
#      保证跨重启的会话间多样性，账本只负责同会话短期避重）。
_RECENT_CAPTION_LOCK = threading.Lock()
_RECENT_CAPTIONS: "OrderedDict[str, tuple]" = OrderedDict()
_RECENT_CAPTIONS_MAX_KEYS = 512   # 会话数上限（LRU 裁剪）
_RECENT_CAPTIONS_PER_KEY = 3      # 每会话记最近 N 条


def _caption_fp(text: Any) -> int:
    """配文指纹（crc32）：近用避重账本存指纹不存原文。"""
    import zlib
    return zlib.crc32(str(text or "").encode("utf-8"))


def recent_captions(chat_key: str) -> tuple:
    """该会话最近真发出过的配文指纹（无记录/空 key 返回空元组）。"""
    k = str(chat_key or "").strip()
    if not k:
        return ()
    with _RECENT_CAPTION_LOCK:
        return tuple(_RECENT_CAPTIONS.get(k) or ())


def note_caption_used(chat_key: str, caption: str) -> None:
    """**发出成功后**记账（挑了没发不记，防空烧避重位）。软失败绝不抛。"""
    try:
        k = str(chat_key or "").strip()
        cap = str(caption or "").strip()
        if not k or not cap:
            return
        fp = _caption_fp(cap)
        with _RECENT_CAPTION_LOCK:
            cur = [x for x in (_RECENT_CAPTIONS.get(k) or ()) if x != fp]
            cur.append(fp)
            _RECENT_CAPTIONS[k] = tuple(cur[-_RECENT_CAPTIONS_PER_KEY:])
            _RECENT_CAPTIONS.move_to_end(k)
            while len(_RECENT_CAPTIONS) > _RECENT_CAPTIONS_MAX_KEYS:
                _RECENT_CAPTIONS.popitem(last=False)
    except Exception:
        logger.debug("[caption] note_caption_used 记账失败（忽略）", exc_info=True)


def pick_caption(pool: Any, chat_key: str, *, now: Any = None,
                 recent: Any = None) -> str:
    """按会话确定性挑配文 + 近用避重（纯选取；账本另经 ``note_caption_used``）。

    种子＝crc32(chat_key + 当日)：不同会话同一天取不同条、同会话同日（无新
    记账时）恒定。``recent`` 显式传入（配文原文或 crc32 指纹的可迭代）则用之，
    否则查进程账本；候选命中近用 → 顺移下一条；全池都近用过 → 按种子取
    （绝不死循环）。任何异常回落纯种子取；池空返回 ""。
    """
    try:
        if isinstance(pool, str):
            items = [pool] if pool.strip() else []
        else:
            items = [str(t) for t in (pool or ()) if str(t or "").strip()]
    except Exception:
        return ""
    if not items:
        return ""
    import datetime as _dt
    import zlib as _zlib
    try:
        t = now if isinstance(now, _dt.datetime) else _dt.datetime.now()
        day = t.strftime("%Y%m%d")
    except Exception:
        day = ""
    try:
        seed = _zlib.crc32(f"{str(chat_key or '')}#{day}".encode("utf-8"))
    except Exception:
        seed = 0
    idx = seed % len(items)
    try:
        used = set()
        src = recent if recent is not None else recent_captions(chat_key)
        for x in (src or ()):
            used.add(_caption_fp(x) if isinstance(x, str) else int(x))
        for step in range(len(items)):
            cand = items[(idx + step) % len(items)]
            if _caption_fp(cand) not in used:
                return cand
        return items[idx]
    except Exception:
        return items[idx]


def selfie_stage_text(key: str, lang: str = "", *, persona_name: str = "",
                      variant_salt: Any = None, chat_key: str = "") -> str:
    """Stage 短路搪塞/兜底/配文文案：按会话语言取 zh/en 模板。

    ``lang`` 为空或 zh* → 中文（产品主语言）；其余一律英文。运营在 config 里
    配置的 ``caption``/``scene_hint`` 等自定义值优先于本函数（调用方先查 config）。
    值为列表 = 变体池：``variant_salt`` 显式传入时确定性取（纯函数口径，测试用）；
    ``chat_key`` 传入时走 ``pick_caption``（crc32(会话+日期) 确定性 + 近用避重，
    2026-08-03 复读治理——**发出成功后**调用方须 ``note_caption_used`` 记账）；
    两者都缺省用进程级轮换游标（随机起步防重启偏置）——同一 key 连续两次触发
    必换措辞（防复读穿帮）。
    ``persona_name`` 仅保留参数兼容：模板已全部第一人称，不再用名字自称。
    """
    entry = _STAGE_TEXTS.get(str(key or ""), {})
    if not entry:
        return ""
    lg = str(lang or "").strip().lower()
    use_zh = (not lg) or lg.startswith("zh")
    tpl = entry["zh"] if use_zh else entry["en"]
    if isinstance(tpl, (list, tuple)):
        if not tpl:
            return ""
        if variant_salt is None and str(chat_key or "").strip():
            tpl = pick_caption(tpl, chat_key) or tpl[0]
        else:
            if variant_salt is None:
                _k = f"{key}:{'zh' if use_zh else 'en'}"
                prev = _STAGE_VARIANT_SEQ.get(_k)
                # 随机起步（2026-08-03）：进程首次触发不再恒取池[0]——频繁重启的
                # 生产机会把池首句放大成高频复读；后续仍逐次 +1（连续必换不变）。
                idx = random.randrange(len(tpl)) if prev is None else prev + 1
                _STAGE_VARIANT_SEQ[_k] = idx
            else:
                idx = int(variant_salt)
            tpl = tpl[idx % len(tpl)]
    name = str(persona_name or "").strip()
    if use_zh:
        name = name or "我"
    elif not name or name == "我":
        name = "me"  # 中文占位名不能漏进英文模板
    try:
        return tpl.format(name=name)
    except Exception:
        return tpl


def build_photo_caption_instruction(
    peer_text: str,
    *,
    kind: str = "selfie",
    subject: str = "",
    persona_name: str = "",
    scene: str = "",
    freshness: str = "fresh",
    wanted_subject: str = "",
    reply_lang: str = "",
) -> str:
    """构造「照片配文」LLM 指令（纯函数）——文图协同的关键一环。

    背景：autosend 的草稿文本是**在不知道会发图**的情况下生成的，常写出
    "等我现在去拍一张？等我一下～"，而图早已附上 → 文图自相矛盾（实录事故）。
    本指令让 LLM 在**明知照片已发出**的前提下补一句贴上下文的配文。
    ``scene``（Phase18）＝这张照片实际的拍摄场景（英文短语，与生图 prompt 同源）：
    给了就让配文自然贴场景（"便利店夜班好无聊～"），图文叙事一体。
    ``freshness``（P0 一致性，2026-07-27）：``fresh``=刚生成的图（可说「刚拍」）；
    ``old``=相册存货（注册相册/相册兜底）——**禁止声称刚拍**（实录事故：相册旧照
    配「刚拍的，新鲜出炉」→ 客户对时间/衣着对不上当场质疑「你真会骗人」），
    改为「翻到一张之前拍的」口径，时间上永远诚实。
    ``wanted_subject``（P0 图文一致性，2026-08-08）＝对方（或 AI 自己的 offer）
    点名想看的**非人像主体**（「燕窝粥」「我的卧室」）——这张图里并没有它时，
    指令显式禁止「假装照片里有」。实录事故：自拍配文写成「给你瞅瞅我卧室的
    样子」「之前拍的存货，家里随便吃吃嘛」，画面里只有人脸。
    ``reply_lang``（#143 0902，接 #74 语言锚）＝会话语言/客户语言画像：非空时
    配文语言**显式钉死**，不再让模型「跟随对方消息的语言」——贴纸/图片轮的
    「对方消息」是系统中文识别标注（[贴纸内容]/[图片内容] …），跟随它＝英文
    会话配出中文文案（「手机里存的这张…」发英文客户实锤）。同理 peer_text
    先剥系统注入行（lang_policy.strip_system_injected），剥空则不引用原话。
    解析/调用在 autosend 侧（保持本模块纯净可测）。
    """
    who = f"「{persona_name}」" if persona_name else "一个真人"
    is_old = str(freshness or "").strip().lower() == "old"
    is_portrait = kind != "object"
    if kind == "object" and subject:
        desc = f"一张「{subject}」的照片"
    elif kind == "video":
        desc = "一段你自己出镜的小视频"
    elif is_old:
        desc = "一张你之前拍的自拍照（存货，画面里是你本人，不是现在拍的）"
    else:
        desc = "一张你刚拍的自拍照"
    # 系统注入的识别标注（[贴纸内容]/[图片内容] 中文描述等）不是对方的话——
    # 引进指令会既误导「相同语言」判断又诱导配文评论识别内容。剥不动时用原文
    # （软失败，与 #74 同一剥离口径）。
    try:
        from src.ai.lang_policy import strip_system_injected as _cap_ssi
        _pt_src = _cap_ssi(str(peer_text or ""))
    except Exception:
        _pt_src = str(peer_text or "")
    pt = _pt_src.strip()[:120]
    sc = str(scene or "").strip()[:120]
    scene_line = (
        f"照片的拍摄场景是：{sc}（配文可自然带到你人在那儿、在做什么，用对话语言"
        "口语化转述，不要输出英文原文；画面主体仍是你本人，别写成在展示那个地方）。\n"
        if sc else "")
    # 人像硬约束：画面里只有人。LLM 拿不到图，只凭对话上下文写配文——不钉死
    # 「这是你的人像照」它就会顺着对话把自拍说成食物/房间/物件的照片。
    portrait_line = (
        "- 这是一张**你本人的人像照**：画面里只有你，没有食物、房间、物品、"
        "风景细节。禁止把它说成别的东西的照片（如「给你看看我的卧室」「这是我做的"
        "XX」「家里的存货」），也不要描述画面里根本没有的东西——可以说你人在哪儿、"
        "在做什么，但不能声称照片里拍到了那样东西\n" if is_portrait else "")
    ws = str(wanted_subject or "").strip()[:40]
    wanted_line = (
        f"- 对方想看的是「{ws}」，但**这张照片里没有它**：绝不能假装这就是{ws}、"
        f"也不要说照片里能看到{ws}；要提就诚实说明这张是你自己，{ws}晚点再补\n"
        if (ws and is_portrait) else "")
    old_line = (
        "- 这张是之前拍的：**禁止**说「刚拍的/刚出炉/现在拍的」这类话，"
        "可以自然带过「翻到一张之前拍的/之前拍的这张」，也不要编造现在的拍摄过程\n"
        if is_old else "")
    _rl = str(reply_lang or "").strip().lower()
    _rl_names = {
        "zh": "中文", "en": "英语", "ja": "日语", "ko": "韩语", "es": "西语",
        "pt": "葡语", "vi": "越南语", "th": "泰语", "id": "印尼语",
        "ru": "俄语", "fr": "法语", "de": "德语", "yue": "粤语",
    }
    if _rl:
        _rl_n = _rl_names.get(_rl.split("-")[0], _rl)
        lang_line = (
            f"- 配文必须用「{_rl_n}」写（这是你们会话一直在用的语言），"
            "禁止使用其它语言\n")
    elif pt:
        lang_line = "- 使用与对方消息相同的语言\n"
    else:
        # 对方本轮是贴纸/图片（无文字）：没有可跟随的语言 → 按会话既有语言
        lang_line = "- 使用你们会话一直在用的语言\n"
    peer_line = f"对方刚才的消息：「{pt}」\n" if pt else ""
    return (
        f"你在聊天软件里扮演{who}（真人口吻）。你刚刚把{desc}发给了对方。\n"
        + peer_line
        + scene_line +
        "写一条随照片一起发送的配文，要求：\n"
        + lang_line +
        "- 口语化、自然、不超过 30 字，最多 1 个 emoji\n"
        "- 照片已经发出去了：禁止写「等我去拍」「我发不了照片」这类否认或拖延的话\n"
        + portrait_line + wanted_line + old_line +
        "- 不要复述对方的话，不要加引号\n"
        "只输出配文正文。"
    )


def decide_selfie(
    *,
    entitlement: Optional[Dict[str, Any]],
    gate_enabled: bool,
    free_used: int,
    free_daily: int,
    bond_level: int = 0,
    min_bond_level: int = 0,
) -> Dict[str, Any]:
    """形象照准入决策（纯函数）。返回 ``{action, feature, used_free}``。

    - ``too_soon``：关系等级不足（避免一上来就要照片的轻浮感）。
    - ``allow``：已拥有相册 / gate 关（不计费）→ 不限；否则免费额度内 → ``used_free=True``。
    - ``locked``：gate 开 + 未拥有 + 免费额度用尽 → 走 exclusive_album 付费引导。
    """
    if int(bond_level) < int(min_bond_level):
        return {"action": "too_soon", "feature": SELFIE_FEATURE, "used_free": False}
    if feature_allowed(entitlement, SELFIE_FEATURE, gate_enabled=bool(gate_enabled)):
        return {"action": "allow", "feature": SELFIE_FEATURE, "used_free": False}
    if int(free_used) < max(0, int(free_daily)):
        return {"action": "allow", "feature": SELFIE_FEATURE, "used_free": True}
    return {"action": "locked", "feature": SELFIE_FEATURE, "used_free": False}


@dataclass
class SelfieResult:
    ok: bool = False
    image_path: str = ""
    prompt: str = ""
    provider: str = ""
    latency_ms: int = 0
    error: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


# album 后端可挑选的图片扩展名。
_ALBUM_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


class SelfieProvider:
    """形象照生成 provider（软失败骨架，镜像 TTSPipeline）。

    Config（``companion.selfie.provider``）：
        enabled: false
        backend: disabled | openai | command
        model/size/api_key/base_url：openai images 用（model=gpt-image-1 默认；亦支持 dall-e-3）
        quality: 可选（gpt-image-1：low|medium|high|auto）；request_timeout_sec: 单请求超时(默认 60)
        command_args / command_template：本地推理（ComfyUI/SD 脚本），占位 {prompt}/{out}
        out_dir: tmp_selfies
    """

    def __init__(self, cfg: Optional[Dict[str, Any]] = None):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.backend = str(cfg.get("backend", "disabled")).strip().lower()
        self.model = str(cfg.get("model") or "gpt-image-1").strip()
        self.size = str(cfg.get("size") or "1024x1024").strip()
        self.quality = str(cfg.get("quality") or "").strip()
        self.api_key = str(cfg.get("api_key") or "").strip()
        self.base_url = str(cfg.get("base_url") or "").strip().rstrip("/")
        self.request_timeout_sec = float(cfg.get("request_timeout_sec", 60) or 60)
        self.out_dir = Path(str(cfg.get("out_dir") or "tmp_selfies"))
        # backend=album：从预制相册随机挑图（不出图、零 API 费、同一张脸最一致）。
        self.album_dir = Path(str(cfg.get("album_dir") or "config/persona_albums"))
        # 分册回落（2026-07-22）：persona 分册无图时先试 default_album_key 分册再根目录
        # ——真机实录：会话人设 lin_jiaxin 无相册，图全在 lin_xiaoyu 分册，
        # album 兜底空手而归。单素材套多人设的测试/小客户部署靠它兜住。
        self.default_album_key = str(cfg.get("default_album_key") or "").strip()
        self.command_args = cfg.get("command_args")
        # 无脸/轻量图（物体图、无 face_ref 自拍）可走独立命令（如指向另一台 ComfyUI），
        # 把重的锁脸自拍留给主卡、分流 GPU。缺省=沿用 command_args（零行为变更）。
        self.command_args_noface = cfg.get("command_args_noface")
        self.command_template = str(cfg.get("command_template") or "").strip()
        self.command_timeout_sec = float(cfg.get("command_timeout_sec", 180) or 180)
        # 生图后端失败时回落相册真图（2026-07-22，默认开；album_fallback: false 关）。
        self.album_fallback = bool(cfg.get("album_fallback", True))

    def stats(self) -> Dict[str, Any]:
        return {"enabled": self.enabled, "backend": self.backend,
                "model": self.model, "out_dir": str(self.out_dir),
                "album_dir": str(self.album_dir)}

    async def generate(
        self, prompt: str, *, timeout_sec: Optional[float] = None,
        album_key: str = "", avoid_path: str = "", base_image: str = "",
        seed: int = -1, lora: str = "", lora_weight: float = 1.0,
        exclude_paths: Any = None,
        allow_album_fallback: Optional[bool] = None,
        album_scene: str = "", now_hour: Optional[int] = None,
        prefer_series: str = "", prefer_scene: str = "",
    ) -> SelfieResult:
        """出图。``base_image`` 非空且存在 → img2img（openai images.edit / command ``{base}``），
        用于锁住人设一致性；album 后端忽略 prompt/base（只挑现成图）。
        ``seed`` ≥0 时透传给 command 后端 ``{seed}`` 占位（人设自拍固定种子稳外观）；
        -1=随机（openai 后端无种子概念，忽略）。
        ``lora``/``lora_weight``（角色 LoRA 部署）：透传给 command 后端 ``{lora}``/
        ``{lora_weight}`` 占位（per-persona 各自的 LoRA 文件）；openai/album 忽略。
        ``exclude_paths``：会话已发过的相册文件（防复读），album 直选与兜底两路都生效。

        P0 图文一致性（2026-07-27）：
        - ``allow_album_fallback``：本次调用级覆盖 ``self.album_fallback``——
          **object（物体图）请求必须传 False**：物体图生成失败绝不能拿相册人像顶包
          （实录事故：要海景/水墨画/抹茶拿铁 → ComfyUI 失败 → 发了张车内自拍配文
          「刚拍的」→ 客户质问"你真会骗人"）。None=沿用实例配置。
        - ``album_scene``：相册挑图（直选与兜底两路）的**场景硬要求**（canonical
          场景类或场景短语，内部归一）：客户/LLM 显式点名场景时挑不到匹配图 →
          如实失败（交诚实文字），绝不发不相干场景的图。空=不限。
        - ``now_hour``：软时段过滤（白天照深夜不发；全冲突时放行但结果
          ``extra["tod_softened"]=True``，配文层须按「之前拍的」口径兜底）。
        - ``prefer_series``：连续性窗口内优先同系列（同套衣服）。
        - ``prefer_scene``（实施69 P1）：**软**场景偏好——泛化要图时尽量贴
          AI 当前叙事场景（scene_state 轮换值），有匹配存货才收窄、没有绝不
          拒发（与 ``album_scene`` 硬语义相对）。非 album 后端忽略（生成链
          场景走 prompt）。
        """
        rv = SelfieResult(prompt=str(prompt or ""), provider=self.backend)
        if not self.enabled or self.backend in ("", "disabled"):
            rv.error = "provider_disabled"
            return rv
        # album 后端：不出图，从预制相册挑一张已有照片（无需 prompt）。
        if self.backend == "album":
            return self._pick_from_album(
                album_key=album_key, avoid_path=avoid_path,
                exclude_paths=exclude_paths, required_scene=album_scene,
                now_hour=now_hour, prefer_series=prefer_series,
                prefer_scene=prefer_scene)
        if not rv.prompt.strip():
            rv.error = "empty_prompt"
            return rv
        # 外层 wait_for 是兜底：须严格大于 client/命令各自的请求超时，否则会在请求合法运行中途
        # 误砍，掩盖掉底层（client.timeout / command_timeout）的精确错误。取请求超时 + 15s 余量。
        inner = (self.command_timeout_sec if self.backend == "command"
                 else self.request_timeout_sec)
        eff_timeout = float(timeout_sec) if timeout_sec else float(inner) + 15.0
        self.out_dir.mkdir(parents=True, exist_ok=True)
        out = self.out_dir / f"selfie-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.png"
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    self._generate_sync, rv.prompt, out, base_image, seed,
                    lora, lora_weight),
                timeout=eff_timeout,
            )
            if out.exists() and out.stat().st_size > 0:
                rv.ok = True
                rv.image_path = str(out)
                rv.extra["bytes"] = out.stat().st_size
                rv.extra["img2img"] = bool(base_image)
            else:
                rv.error = "empty_image"
        except asyncio.TimeoutError:
            rv.error = f"selfie_timeout({eff_timeout:.0f}s)"
        except Exception as ex:  # noqa: BLE001
            rv.error = f"{type(ex).__name__}: {ex}"
        rv.latency_ms = int((time.monotonic() - t0) * 1000)
        # ── 生图失败 → 相册兜底（2026-07-22，album_fallback 默认开）──────────
        # 真机场景：176 GPU 被占/ComfyUI 挂/超时 → 与其回落"发不出图找借口"，
        # 不如从人设相册挑一张真图兑现（策展相册的图本就是该人设的"本人照片"）。
        # 相册空/仍失败 → 保留原错误原样返回（外层照旧走文字兜底）。
        # P0（2026-07-27）：兜底继承 album_scene/now_hour 硬软过滤——点名场景
        # 挑不到匹配图时如实失败，不再随机顶包（「要海景发车内自拍」事故收口）。
        _fb_on = (self.album_fallback if allow_album_fallback is None
                  else bool(allow_album_fallback))
        if not rv.ok and _fb_on:
            fb = self._pick_from_album(
                album_key=album_key, avoid_path=avoid_path,
                exclude_paths=exclude_paths, required_scene=album_scene,
                now_hour=now_hour, prefer_series=prefer_series,
                prefer_scene=prefer_scene)
            if fb.ok:
                fb.prompt = rv.prompt
                fb.extra["fallback_from"] = self.backend
                fb.extra["primary_error"] = rv.error
                fb.latency_ms = rv.latency_ms
                logger.info(
                    "[selfie] 生图后端 %s 失败(%s) → 相册兜底命中",
                    self.backend, rv.error)
                return fb
            if fb.error:
                rv.extra["album_fallback_error"] = fb.error
        return rv

    def _album_dirs(self, album_key: str = "") -> list:
        """候选相册目录：``album_dir/<persona_key>`` →（配置了则）``album_dir/<default_album_key>``
        → ``album_dir`` 根目录，取第一个有图的（见 ``_list_album``）。

        ``album_key`` 只保留字母/数字（含 CJK）/``-``/``_``——挡掉路径分隔符与 ``..``，防目录穿越。
        """
        dirs: list = []
        key = "".join(c for c in str(album_key or "") if c.isalnum() or c in ("-", "_"))
        if key:
            dirs.append(self.album_dir / key)
        dkey = "".join(c for c in self.default_album_key if c.isalnum() or c in ("-", "_"))
        if dkey and dkey != key:
            dirs.append(self.album_dir / dkey)
        dirs.append(self.album_dir)
        return dirs

    def _root_has_sub_albums(self) -> bool:
        """相册根目录下是否存在「含图的人设分册」（多人设分册布局判定）。"""
        try:
            for sub in self.album_dir.iterdir():
                if not sub.is_dir():
                    continue
                for p in sub.iterdir():
                    if p.is_file() and p.suffix.lower() in _ALBUM_IMAGE_EXT:
                        return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def _list_album(self, album_key: str = "") -> list:
        """列出候选目录里第一个含图片的目录的所有图片路径（排序稳定）；无则空列表。

        跨人设隔离（P0，2026-07-27）：``album_key`` 非空（人设已知）且根目录下
        存在**别的人设分册**（多人设布局）时，**根目录候选不参与回落**——多人设
        部署里根目录散图来源不明，静默发出等于「换脸换人」（发 A 人设时挑到
        B 人设/遗留素材）。显式共享素材包用 ``default_album_key``（运营 opt-in，
        2026-07-22 语义保持不变）；单人设平铺布局（根目录放图、无分册）不受影响。
        """
        key = "".join(c for c in str(album_key or "") if c.isalnum() or c in ("-", "_"))
        _root = None
        try:
            _root = self.album_dir.resolve()
        except Exception:  # noqa: BLE001
            _root = self.album_dir
        for d in self._album_dirs(album_key):
            try:
                _is_root = False
                try:
                    _is_root = (d.resolve() == _root)
                except Exception:  # noqa: BLE001
                    _is_root = (d == self.album_dir)
                if _is_root and key and self._root_has_sub_albums():
                    continue  # 多人设布局：根目录不做跨人设兜底
                if d.is_dir():
                    files = sorted(
                        str(p) for p in d.iterdir()
                        if p.is_file() and p.suffix.lower() in _ALBUM_IMAGE_EXT
                    )
                    if files:
                        return files
            except Exception:  # noqa: BLE001
                continue
        return []

    def _pick_from_album(
        self, *, album_key: str = "", avoid_path: str = "",
        exclude_paths: Any = None, required_scene: str = "",
        now_hour: Optional[int] = None, prefer_series: str = "",
        prefer_scene: str = "",
    ) -> SelfieResult:
        """从预制相册随机挑一张（尽量避开上一张 ``avoid_path``，避免连发同图）。

        ``exclude_paths``（2026-07-22 防复读）：该会话已发过的文件路径/文件名集合
        ——分层回落：先排除已发+同系列 → 只排已发 → 全发过用全池（绝不拒发）。
        系列从策展命名解析（``<scene>_<series>_<nn>.jpg``）。face_ref 不参与投放
        （那是锁脸基准照，发出去会和生成图"同脸同构图"穿帮）。

        P0 图文一致性（2026-07-27，元数据来自目录 ``_meta.json`` sidecar +
        文件名约定，见 ``album_file_meta``）：
        - ``required_scene``：场景硬要求（短语/场景类均可，内部归一）——挑不到
          匹配场景的图 → ``ok=False, error=album_scene_mismatch``（如实失败）；
        - ``now_hour``：时段软过滤（剔 ``tod`` 冲突项；剔空放行 +
          ``extra["tod_softened"]=True``，配文层须按「之前拍的」口径兜底）；
        - ``prefer_series``：连续性窗口内优先同系列未发文件（同套衣服再拍一张）。
        """
        files = [f for f in self._list_album(album_key)
                 if Path(f).stem.lower() != "face_ref"]
        if not files:
            return SelfieResult(provider="album", error="album_empty")
        meta = load_album_meta(Path(files[0]).parent)
        fmeta = {f: album_file_meta(f, meta) for f in files}
        pool = files
        # 场景硬要求：点名要海边就只出海边；场景未知的图不算匹配（宁缺勿错）。
        if str(required_scene or "").strip():
            from src.companion.persona_media import scene_class_of
            want = scene_class_of(required_scene) or str(
                required_scene).strip().lower()
            pool = [f for f in pool if fmeta[f]["scene"] == want]
            if not pool:
                return SelfieResult(
                    provider="album", error="album_scene_mismatch",
                    extra={"required_scene": want, "album_size": len(files)})
        # 时段软过滤：白天照深夜不发（有标注才判）；剔空放行但标记 softened。
        tod_softened = False
        if now_hour is not None:
            from src.companion.persona_media import tod_conflicts_with_hour
            lit = [f for f in pool
                   if not tod_conflicts_with_hour(fmeta[f]["tod"], now_hour)]
            if lit:
                pool = lit
            else:
                tod_softened = True
        pool = [f for f in pool if f != str(avoid_path or "")] or pool
        ex = {str(p) for p in (exclude_paths or ())}
        ex_names = {Path(p).name for p in ex}
        # 服装连续性：短窗口内「再拍一张」优先同系列未发文件。
        _series_hit = False
        if prefer_series:
            same = [f for f in pool
                    if fmeta[f]["series"] == prefer_series
                    and Path(f).name not in ex_names]
            if same:
                pool = same
                _series_hit = True
        # 场景软偏好（实施69 P1）：泛化要图尽量贴 AI 当前叙事场景（scene_state
        # 轮换值）——「嘴上说在夜市，发的图在客厅」是 2026-08-24 实录第一穿帮点。
        # 软语义：有匹配存货才收窄、没有绝不拒发（与 required_scene 硬语义相对）；
        # 连续窗系列命中时让位（「再拍一张」时刚发那张的场景才是现场真相，轮换值
        # 不该打断同景同装）；时段过滤已在前（深夜偏好夜景库存而非硬塞白天街拍）。
        _scene_pref_hit = ""
        if (not _series_hit and not str(required_scene or "").strip()
                and str(prefer_scene or "").strip()):
            from src.companion.persona_media import scene_class_of
            _pref = scene_class_of(prefer_scene) or str(
                prefer_scene).strip().lower()
            liked = [f for f in pool if fmeta[f]["scene"] == _pref]
            if liked:
                pool = liked
                _scene_pref_hit = _pref
        if ex:
            sent_series = {album_series_of_path(p) for p in ex}
            sent_series.discard("")
            fresh = [f for f in pool
                     if Path(f).name not in ex_names
                     and (album_series_of_path(f) not in sent_series
                          or not album_series_of_path(f))]
            unsent = [f for f in pool if Path(f).name not in ex_names]
            pool = fresh or unsent or pool
        pick = random.choice(pool)
        _pm = fmeta.get(pick) or album_file_meta(pick, meta)
        # 绝对路径（2026-07-22 真机复盘）：album_dir 常为相对路径（相对引擎 cwd），
        # 相对路径经编排器发给 Node sidecar（cwd 不同）时 readFileSync ENOENT →
        # WhatsApp 相册图 100% 发送失败。resolve 一处，所有消费方受益。
        try:
            pick = str(Path(pick).resolve())
        except OSError:
            pass
        return SelfieResult(ok=True, image_path=pick, provider="album",
                            extra={"album_size": len(files),
                                   "series": _pm["series"],
                                   "scene_class": _pm["scene"],
                                   "tod": _pm["tod"],
                                   "tod_softened": tod_softened,
                                   "scene_pref_hit": _scene_pref_hit})

    def reference_image(self, album_key: str = "") -> str:
        """挑一张相册图当"基础图/锁脸参考"（openai/command 后端 img2img 用）；无相册回空串。

        让 album_dir 一物两用：album 后端直接发它，openai/command 后端拿它当 img2img 基础图，
        使生成的人设照片保持同一张脸。

        优先选名为 ``face_ref.*`` 的"官方基准脸"（PuLID 锁脸约定，运营手工定妆的那张，
        不随相册增删漂移）；没有才回落排序第一张。
        """
        files = self._list_album(album_key)
        for f in files:
            if Path(f).stem.lower() == "face_ref":
                return f
        return files[0] if files else ""

    def _generate_sync(
        self, prompt: str, out: Path, base_image: str = "", seed: int = -1,
        lora: str = "", lora_weight: float = 1.0,
    ) -> None:
        if self.backend == "openai":
            self._generate_openai(prompt, out, base_image)
            return
        if self.backend == "command":
            self._generate_command(prompt, out, base_image, seed, lora, lora_weight)
            return
        raise RuntimeError(f"unknown backend {self.backend}")

    def _generate_openai(self, prompt: str, out: Path, base_image: str = "") -> None:
        client = self._make_openai_client()
        if base_image and Path(base_image).is_file():
            out.write_bytes(self._openai_edit_bytes(client, prompt, base_image))
        else:
            out.write_bytes(self._openai_generate_bytes(client, prompt))

    def _make_openai_client(self) -> Any:
        """构造 OpenAI 客户端（独立测试缝：测试可 monkeypatch 本方法注入假 client）。"""
        from openai import OpenAI  # type: ignore
        if not self.api_key:
            raise RuntimeError("missing api_key for openai images")
        kwargs: Dict[str, Any] = {"api_key": self.api_key,
                                  "timeout": self.request_timeout_sec}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return OpenAI(**kwargs)

    def _openai_generate_bytes(self, client: Any, prompt: str) -> bytes:
        """调 images.generate 并取回 PNG 字节。model 感知 + b64/url 双回退。

        - ``gpt-image-1``：恒返回 b64（且**不接受** response_format 参数，传了会报错）。
        - ``dall-e-2/3``：默认返回 url；显式要 ``response_format=b64_json`` 才回 b64。
        - 兜底：拿不到 b64 但有 url → 下载 url（兼容自建/代理 images 网关行为差异）。
        """
        req: Dict[str, Any] = {"model": self.model, "prompt": prompt,
                               "size": self.size, "n": 1}
        if self.quality:
            req["quality"] = self.quality
        if self.model.startswith("dall-e"):
            req["response_format"] = "b64_json"
        return self._resp_to_bytes(client.images.generate(**req))

    def _openai_edit_bytes(self, client: Any, prompt: str, base_image: str) -> bytes:
        """基础图 img2img：调 images.edit（gpt-image-1 / dall-e-2 编辑接口），锁住人设一致性。

        传入基础图 + prompt，返回改写后的图；b64/url 双回退同 generate（dall-e-2 需 b64_json）。
        """
        req: Dict[str, Any] = {"model": self.model, "prompt": prompt,
                               "size": self.size, "n": 1}
        if self.model.startswith("dall-e"):
            req["response_format"] = "b64_json"
        with open(base_image, "rb") as fh:
            req["image"] = fh
            resp = client.images.edit(**req)
        return self._resp_to_bytes(resp)

    def _resp_to_bytes(self, resp: Any) -> bytes:
        """从 images 响应（generate/edit 通用）取 PNG 字节：优先 b64_json，回退下载 url。"""
        import base64 as _b64

        data = getattr(resp, "data", None) or []
        if not data:
            raise RuntimeError("openai images: empty response data")
        item = data[0]
        b64 = getattr(item, "b64_json", None) if not isinstance(item, dict) \
            else item.get("b64_json")
        if b64:
            return _b64.b64decode(b64)
        url = getattr(item, "url", None) if not isinstance(item, dict) \
            else item.get("url")
        if url:
            return self._download_image(str(url))
        raise RuntimeError("openai images: no b64_json/url in response")

    def _download_image(self, url: str) -> bytes:
        """下载远端图片（stdlib，不引依赖）；受 request_timeout_sec 约束。"""
        import urllib.request

        with urllib.request.urlopen(url, timeout=self.request_timeout_sec) as r:
            data = r.read()
        if not data:
            raise RuntimeError("openai images: empty download")
        return data

    @staticmethod
    def command_error_excerpt(stderr: str, stdout: str, limit: int = 480) -> str:
        """子进程失败给上层的错误摘录：**取尾部**而非头部。

        comfy_infer 的 stderr 先打显存腾挪等过程日志、真正的终局错误在最后——
        2026-08-22 实锤：UI/服务端日志只见开头 300 字「显存不足…」，真因
        「ckpt not in []（服务端模型清单为空）」被整段截掉，排障只能上 176 翻
        _comfy.log。摘录规则：末 3 行非空日志 + 显式 ``ERR_CODE=`` 行（若不在
        末 3 行内则前置），封顶 ``limit`` 字符（从尾部保留）。
        """
        text = (stderr or stdout or "").strip()
        if not text:
            return ""
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        code_line = next((ln for ln in reversed(lines) if "ERR_CODE=" in ln), "")
        tail_lines = lines[-3:]
        parts = ([code_line] if code_line and code_line not in tail_lines else []) + tail_lines
        out = " | ".join(parts)
        return out[-limit:] if len(out) > limit else out

    def _generate_command(
        self, prompt: str, out: Path, base_image: str = "", seed: int = -1,
        lora: str = "", lora_weight: float = 1.0,
    ) -> None:
        raw_args = self.command_args
        # 无脸/轻量图（无 base_image）+ 配了 command_args_noface（list）→ 走它分流 GPU
        # （如物体图/无锁脸自拍指向另一台 ComfyUI）。template 形态不做 noface 路由（保持简单）。
        if not str(base_image or "").strip() and isinstance(self.command_args_noface, list):
            raw_args = self.command_args_noface
        tpl = self.command_template
        if not tpl and not isinstance(raw_args, list):
            raise RuntimeError("selfie command not configured")
        # {base}=基础图路径（img2img，空则为空串——脚本可据此决定 text2img/img2img）；
        # {seed}=生成种子（-1=脚本自选随机；人设自拍传固定值稳外观）；
        # {lora}/{lora_weight}=per-persona 角色 LoRA（空=不挂；命令模板未含占位则忽略）。
        values = {"prompt": prompt, "out": str(out), "base": str(base_image or ""),
                  "seed": str(int(seed)), "lora": str(lora or ""),
                  "lora_weight": str(lora_weight)}
        # text 解码显式 UTF-8 + errors=replace：子进程（comfy_infer）stderr 是 UTF-8，
        # 而 Windows 默认按 GBK 解码 → 中文报错行抛 UnicodeDecodeError 打断线程读取
        # （2026-07-14 实测噪声）。统一 UTF-8 容错解码。
        if isinstance(raw_args, list):
            cmd = [str(x).format(**values) for x in raw_args]
            r = subprocess.run(cmd, shell=False, capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               timeout=self.command_timeout_sec, env=os.environ.copy())
        else:
            quoted = {k: shlex.quote(v) for k, v in values.items()}
            r = subprocess.run(tpl.format(**quoted), shell=True, capture_output=True,
                               text=True, encoding="utf-8", errors="replace",
                               timeout=self.command_timeout_sec,
                               env=os.environ.copy())
        if r.returncode != 0:
            # 完整输出（尾 4000 字）落服务端日志——摘录只保终局错误，全量真相
            # 必须有处可查（此前两头都截断，事故只能靠上出图机翻日志定位）。
            logger.warning("[selfie_command] rc=%s 输出尾部: %s", r.returncode,
                           ((r.stderr or "") + (r.stdout or ""))[-4000:])
            raise RuntimeError(
                "selfie_command_failed:"
                f"{self.command_error_excerpt(r.stderr, r.stdout)}")


_selfie_singleton: Optional[SelfieProvider] = None
_selfie_cfg_key: str = ""


def get_selfie_provider(cfg: Optional[Dict[str, Any]] = None) -> SelfieProvider:
    """配置指纹变了就重建（2026-07-22 配置单例审计第 3 处；与 get_orchestrator/
    get_autoreply_limiter 同款缺陷：热重载改 provider 配置后单例仍用旧 backend）。"""
    global _selfie_singleton, _selfie_cfg_key
    key = repr(sorted((str(k), repr(v)) for k, v in (cfg or {}).items()))
    if _selfie_singleton is None or (cfg and key != _selfie_cfg_key):
        _selfie_singleton = SelfieProvider(cfg or {})
        _selfie_cfg_key = key
    return _selfie_singleton


def reset_selfie_provider() -> None:
    global _selfie_singleton
    _selfie_singleton = None


__all__ = [
    "SELFIE_FEATURE",
    "detect_selfie_request",
    "detect_media_complaint",
    "extract_requested_scene",
    "wants_same_scene",
    "build_selfie_prompt",
    "selfie_variety",
    "resolve_variety_salt",
    "resolve_persona_lora",
    "load_lora_registry",
    "stable_selfie_seed",
    "scene_pool",
    "pick_scene_hint",
    "build_scene_choice_instruction",
    "parse_scene_choice",
    "build_photo_caption_instruction",
    "selfie_stage_text",
    "pick_caption",
    "note_caption_used",
    "recent_captions",
    "album_scene_class_of_path",
    "album_series_of_path",
    "load_album_meta",
    "album_file_meta",
    "decide_selfie",
    "SelfieResult",
    "SelfieProvider",
    "get_selfie_provider",
    "reset_selfie_provider",
]
