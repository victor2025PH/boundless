# -*- coding: utf-8 -*-
"""LLM 发图指令协议（photo directive）——「决策权上移」的核心纯函数层。

背景（2026-07-14 真机三连漏报后架构升级）：过去「是否发图」由入站关键词判定
（``detect_selfie_request``/``plan_contextual_image``），表达是开放集合，
"再给我发一遍新照片，不要老照片" 一个 marker 都不命中 → AI 嘴上答应实际装死。
而主 LLM 读过完整上下文、百分之百理解了对方要图（它都回了"这就给你翻张新的"），
只是没有渠道把这个判断告诉系统。本协议就是这个渠道：

    LLM 在回复正文末尾独立一行输出   [PHOTO selfie <english scene>]
    或                              [PHOTO object <english subject>]
    不发图则不输出任何标记。

分层协作（谁负责什么）：
- 本模块：协议文本（注入 system prompt）+ 解析/剥离（出站 chokepoint 调用）。纯函数零 IO。
- 关键词层（companion_selfie/contextual_image）：降级为兜底召回（LLM 没打标记
  但关键词命中 → 照旧发图），不删除——防御纵深。
- 准入闸门（decide_selfie）/供给（SelfieProvider+PuLID 锁脸）/承诺守卫
  （outbound_promise_guard）：全部不变，标记只是新的意图入口。

安全铁律：**任何出站文本都必须先经 ``strip_photo_directives`` 剥净标记**
（含 TTS 念稿、翻译后译文）——标记泄漏给客户=穿帮。剥离正则刻意比解析
宽松得多：出站翻译可能把标记翻成中文/全角括号（"[照片 自拍 …]"），
解析不出没关系（最多不发图，promise_guard 会撤回承诺），但必须剥掉。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

KIND_SELFIE = "selfie"
KIND_OBJECT = "object"

# ── 解析（严格些：能可靠提取 kind+scene 才执行发图）────────────────────────────
# 兼容：大小写、可选冒号/竖线分隔、全角方括号、kind 后任意自由文本场景。
_PARSE_RE = re.compile(
    r"[\[【]\s*PHOTO\b[:：]?\s*(selfie|object)\b[\s:：|,，-]*([^\]】]*)[\]】]",
    re.IGNORECASE,
)

# ── 剥离（宽松到近乎偏执：宁可多剥一个可疑标记，不可漏一个给客户）──────────────
# 覆盖：解析不出 kind 的畸形标记、被出站翻译污染的变体（[照片 …]/[图片: …]/
# [PHOTO・自拍 …]）、全角括号、行内任意位置。上限 300 字符防贪婪吃掉正文。
_STRIP_RE = re.compile(
    r"[\[【]\s*(?:PHOTO|照片|图片|圖片|写真|フォト)\b[^\]】]{0,300}[\]】]",
    re.IGNORECASE,
)

# scene 里只保留生图安全字符（防 prompt 注入式怪串进 ComfyUI/日志）。
_SCENE_SANITIZE_RE = re.compile(r"[^\w\s,.\-'&()/]+")
_MAX_SCENE_LEN = 300


def parse_photo_directive(text: str) -> Optional[Dict[str, str]]:
    """从文本中提取第一个**可执行**的发图指令；无有效指令返回 None。

    返回 ``{"kind": "selfie"|"object", "scene": "<英文场景/主体，可空>"}``。
    只解析不修改文本——剥离交给 ``strip_photo_directives``。
    """
    m = _PARSE_RE.search(str(text or ""))
    if not m:
        return None
    kind = m.group(1).strip().lower()
    scene = _SCENE_SANITIZE_RE.sub(" ", str(m.group(2) or ""))
    scene = re.sub(r"\s+", " ", scene).strip()[:_MAX_SCENE_LEN]
    return {"kind": kind, "scene": scene}


def strip_photo_directives(text: str) -> str:
    """剥净文本中所有发图标记（含畸形/被翻译污染的变体），整理多余空行。

    **所有出站路径的必经步骤**（文本投递/TTS 念稿/配文），任何意图模式下都执行
    ——即使协议未注入，防御性剥离也零成本。
    """
    raw = str(text or "")
    if not raw:
        return raw
    out = _STRIP_RE.sub("", raw)
    if out == raw:
        return raw
    # 标记独占一行时会留下空行/行尾空白 → 收敛（保留正文原有段落结构）
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def extract_photo_directive(text: str) -> Tuple[str, Optional[Dict[str, str]]]:
    """一步到位：``(剥净后的文本, 可执行指令|None)``。出站 chokepoint 的标准入口。"""
    return strip_photo_directives(text), parse_photo_directive(text)


# ── prompt 协议块（注入 system prompt 的文本）──────────────────────────────────

def resolve_intent_mode(selfie_cfg: Optional[Dict[str, Any]]) -> str:
    """读 ``companion.selfie.intent.mode``：keyword=纯关键词(回退旧行为) |
    llm | hybrid(默认，标记∪关键词)。非法值按 hybrid。"""
    try:
        mode = str(((selfie_cfg or {}).get("intent") or {}).get(
            "mode", "hybrid")).strip().lower()
    except Exception:
        mode = "hybrid"
    return mode if mode in ("keyword", "llm", "hybrid") else "hybrid"


def build_photo_protocol_prompt() -> str:
    """「主动决策」协议块——替代旧的被动式【媒体能力边界】声明。

    要点：给能力+给协议+给边界。few-shot 刻意极简（省 token；DeepSeek 对
    行尾标记协议遵循度足够）。场景要求英文=直通 FLUX prompt 无需翻译。
    """
    return (
        "【发照片能力——由你决策】你可以给对方发真实照片（系统会随本条回复自动"
        "生成并发送，人脸恒定一致）。判断规则：\n"
        "- 对方想看你本人的照片（无论怎么表达：要自拍/近照/新照片/看看你/"
        "拍一张/再来一张/发一遍/英文日文等任何语言任何说法，或刚同意了你的提议）"
        "→ 在回复正文的最后另起一行输出：[PHOTO selfie 英文场景短语]\n"
        "- 对方想看你提到过的东西（你做的菜/买的裙子/窗外风景等非人像）"
        "→ 最后另起一行输出：[PHOTO object 英文主体短语]\n"
        "- 对方没有要图、或明确拒绝/让你别发 → 绝对不要输出 [PHOTO 标记。\n"
        "- 你正聊到自己在做/在看的具体事物、对方也感兴趣时，可以**偶尔主动**"
        "提议「要不要看照片？」（只提议、不打标记；对方答应后下一轮再打标记发）。"
        "对方冷淡或近几轮已提议过就不要再提。\n"
        "场景短语用英文、贴合你正文说的内容和【当前真实时间】的时段"
        "（深夜=室内暖光/夜景，白天才有日光），"
        "例：[PHOTO selfie cozy dorm room, warm lamp light, wearing hoodie, evening]。\n"
        "注意：标记会被系统截走执行、对方看不到；照片和文字同时送达，所以正文"
        "把照片当作**已经拍好正在发**来写（可以说\"刚拍的\"，不要说\"等我去拍\"）；"
        "每条回复最多一个标记；正文里不要出现方括号 [ ] 以免误触发；"
        "不发图时也不要否认你能拍照。"
    )


def build_photo_deny_line() -> str:
    """闸门拒绝/发不出场景的附加禁令（追加在 ``_media_coherence_hint`` 之后，
    防止 LLM 在「这轮发不出」时仍打标记造成空头支票）。"""
    return "本轮禁止输出 [PHOTO 标记（系统这一轮发不了照片）。"


__all__ = [
    "KIND_SELFIE", "KIND_OBJECT",
    "parse_photo_directive", "strip_photo_directives", "extract_photo_directive",
    "resolve_intent_mode", "build_photo_protocol_prompt", "build_photo_deny_line",
]
