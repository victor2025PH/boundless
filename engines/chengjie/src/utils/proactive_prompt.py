"""主动外发文案生成 prompt 组装（确定性纯函数，可单测）。

把「要发出去的那一句」的 prompt 拼装从 main.py 闭包里抽出来——既可单测，也修掉 Stage L
引入的**框定错配**：此前无论沉默回访还是每日仪式，都套同一句「正在主动给一位**许久未联系**的
朋友发消息」。对晨/晚安这种**每天到点的日常问候**，这个「久别重逢」框定会把文案带偏
（生成出「好久不见」式的生分感）。本模块按 ``plan.mode`` 给出贴合的框定：
- ``ritual_morning`` / ``ritual_night`` → 「每天都会惦记 TA 的人，发一句平常的早/晚安」
- 其余（follow_up / gentle_checkin / story_*）→ 按**真实沉默时长**分档框定
  （P0 2026-07-29：此前一律「许久未联系」，沉默 4 小时也被框成久别重逢 →
  LLM 稳定产出「好久没联系」开场；现在几小时/几天/几周/久别各说各话）。

只拼 prompt、零 IO、不调 AI；真实文案由上层把本串喂给 ``ai_client.chat`` 产出。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.utils.proactive_topic import (
    GAP_FEW_DAYS,
    GAP_LONG,
    GAP_SAME_DAY,
    GAP_WEEK,
    silence_gap_bucket,
    silence_gap_phrase,
)

_RITUAL_SLOT_LABEL = {"ritual_morning": "早安", "ritual_night": "晚安"}


def _silence_header(name: str, plan: Dict[str, Any]) -> str:
    """沉默回访类开场的框定：按真实沉默时长说人话，短档明令禁「久别重逢」腔。"""
    bucket = str(plan.get("gap_bucket") or "")
    silent_hours = plan.get("silent_hours")
    if not bucket:
        # 未带档位的旧调用方 / ask_*、story_* 等 opener：有 silent_hours 就现算，
        # 连时长都没有则保守按「几天没聊」框定（绝不默认久别重逢）。
        bucket = (silence_gap_bucket(silent_hours)
                  if silent_hours is not None else GAP_WEEK)
    gap = (f"（你们上次聊天距现在{silence_gap_phrase(silent_hours)}）"
           if silent_hours else "")
    if bucket == GAP_SAME_DAY:
        return (
            f"你是「{name}」，今天才和TA聊过，这会儿又想到TA，主动再说句话{gap}。"
            "这不是久别重逢——绝不要用「好久没联系/好久不见」这类措辞。"
        )
    if bucket == GAP_FEW_DAYS:
        return (
            f"你是「{name}」，想主动给一两天没聊的TA发条消息{gap}——只是日常惦记，"
            "不是久别重逢，别用「好久没联系」这类措辞。"
        )
    if bucket == GAP_LONG:
        return f"你是「{name}」，正在主动给一位许久未联系的朋友发消息{gap}。"
    return (
        f"你是「{name}」，想主动给几天没聊的TA发条消息{gap}——像朋友忽然想起TA，"
        "不是久别重逢的生分口吻。"
    )


def build_proactive_prompt(
    ai_name: str,
    plan: Dict[str, Any],
    *,
    recent_context: str = "",
    few_shot_block: str = "",
    peer_language: str = "",
    scene_note: str = "",
    persona_style: str = "",
    avoid_texts: Optional[List[str]] = None,
) -> str:
    """组装主动外发文案生成 prompt（按 mode 自适应框定）。绝不抛。

    Args:
        ai_name: AI 人设名。
        plan: 发送计划，至少含 ``directive``；可选 ``mode`` / ``context_facts`` /
            ``gap_bucket`` / ``silent_hours``（沉默类开场按后两者框定真实时长）。
        recent_context: 最近聊天上下文（已截断），供参考口吻，可空。
        few_shot_block: 人工认可样本拼成的风格示范块（见 build_few_shot_block），可空。
        peer_language: 对端会话语言代码（如 ``en``/``ja``；inbox conversations.language）。
            非空且非中文 → prompt 里硬性要求用该语言写（修真机事故：给全程说英文的
            客户发中文开场+中文语音，一眼机器人）。
        scene_note: 生活照场景（英文短语，Phase17 文案-场景对齐）。非空表示本条消息
            会附一张"你在该场景的自拍"——提示 LLM 自然带到正在做的事，图文一体。
        persona_style: 人设说话风格一行（personality.style / style_hint），可空。
            主动消息与被动回复应是同一个「人」——此前只带名字，七个人设写出同一句
            「好久没联系啦」。
        avoid_texts: 「禁止相似」负样本——最近主动发过但没得到回应的开场原文。
            LLM 必须换切入点/句式（P0 反复读：生产实锤同句式 x3/x2/x2 连发）。
    """
    name = str(ai_name or "她")
    plan = plan or {}
    mode = str(plan.get("mode") or "")
    directive = str(plan.get("directive") or "")

    if mode.startswith("ritual_"):
        slot = _RITUAL_SLOT_LABEL.get(mode, "问候")
        header = (
            f"你是「{name}」，正在像一个每天都会惦记着TA的人那样，给TA发一句平常的"
            f"{slot}问候——不是久别重逢，就是日常里每天一句的牵挂。"
        )
        length = "不超过30字"
    elif mode.startswith("milestone_"):
        # 纪念日/节日：具体场合由 directive 承载，这里只给「为特别的日子发问候」的框定，
        # 同样避开「久别重逢」误导（节点是惦记着重要日子，不是好久没联系）。
        header = (
            f"你是「{name}」，正在为一个对你们有意义的特别日子，主动给TA发一句应景的"
            f"问候（具体是什么日子见下方指令，按它来）。"
        )
        length = "不超过40字"
    else:
        header = _silence_header(name, plan)
        length = "不超过40字"

    prompt = (
        f"{header}\n{directive}\n"
        f"要求：只输出要发出去的那一句话本身，口语化、温暖、自然，{length}，"
        f"不要解释、不要加引号、不要署名。\n"
    )

    ps = str(persona_style or "").strip()
    if ps:
        prompt += f"（你的说话风格：{ps}——按这个风格说，但别提「风格」本身。）\n"

    avoid = [str(t).strip() for t in (avoid_texts or []) if str(t).strip()][:4]
    if avoid:
        prompt += (
            "\n（⚠ 你之前主动发过下面这些开场，TA 还没有回应。这次必须换一个"
            "完全不同的切入点和句式——不要再用「问最近怎么样/还好吗」的问候壳子，"
            "意思或句式与它们雷同的话都不要说）：\n- " + "\n- ".join(avoid) + "\n"
        )

    sc = str(scene_note or "").strip()
    if sc:
        prompt += (
            f"（这条消息会随手附一张你刚拍的自拍，拍摄场景是：{sc}。"
            "请把「你此刻正在那儿/正在做的事」自然融进这句话里，像顺手分享日常，"
            "不要写「给你看照片」「如图」这类词。）\n"
        )

    lang = str(peer_language or "").strip().lower()
    if lang and lang not in ("zh", "zh-cn", "zh-tw", "zh-hans", "zh-hant", "unknown"):
        _names = {"en": "英语", "ja": "日语", "ko": "韩语", "th": "泰语",
                  "vi": "越南语", "id": "印尼语", "ms": "马来语", "es": "西班牙语",
                  "pt": "葡萄牙语", "fr": "法语", "de": "德语", "ru": "俄语",
                  "ar": "阿拉伯语"}
        _label = _names.get(lang, lang)
        prompt += (
            f"（TA 平时用{_label}和你聊天——这句话必须用{_label}写，"
            f"绝不要用中文。）\n"
        )

    facts = [
        str(f).strip() for f in (plan.get("context_facts") or []) if str(f).strip()
    ]
    if facts:
        prompt += (
            "\n（背景：你还记得关于TA的这些事，仅用来把这一句说得更走心，"
            "绝不要罗列、不要逐条追问）：\n- " + "\n- ".join(facts[:3]) + "\n"
        )
    if recent_context:
        prompt += (
            "\n（下面是你们最近的聊天记录，「你」开头的是你自己说过的话。"
            f"参考语境和称呼习惯，但不要复读原话）：\n{recent_context}\n")
    if few_shot_block:
        prompt += few_shot_block
    return prompt


__all__ = ["build_proactive_prompt"]
