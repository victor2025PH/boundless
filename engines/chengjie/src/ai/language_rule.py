# -*- coding: utf-8 -*-
"""会话语言规则块（B6，2026-09-11）——从 ai_client._build_system_instruction 抽出。

旧块实测 600–2700 字（英文会话还中英双语各说一遍「禁止输出中文 / 必须翻译模板」）。
本模块压到 ≤400 字，语义不变：

  · 非中文：钉死目标语、禁夹中文（ja/ko 禁中文句子；拉丁语禁任何汉字）、模板先译、命令原样；
  · 中文会话：跟用户语言走（#1），模板先译；电商域才保留通道名不译。

``ai_client`` 装配时调用 :func:`language_rule_block`；本文件零依赖、可单测。
"""
from __future__ import annotations

from typing import Optional

# 门禁钉住：装配出的块（含标题）不超过这个字数。再长就回到「保护名单救一块 2.7k 尾巴」。
MAX_CHARS = 400


def language_rule_block(reply_lang: str, lang_name: str, *,
                        companion: bool = False) -> str:
    """返回要注入 system 的整段（含【标题】）。``reply_lang`` 空/zh 且无 lang_name → 中文会话规则。"""
    lang = str(reply_lang or "zh").strip() or "zh"
    name = str(lang_name or "").strip()
    if lang != "zh" and name:
        extra = _no_zh_extra(lang, companion=companion)
        return (
            f"【LANGUAGE RULE — TOP PRIORITY — MANDATORY】"
            f"Reply ENTIRELY in {name}.{extra} "
            f"Switch now if earlier turns differ — do not second-guess from history. "
            f"Translate Chinese templates/KB first. Keep commands (/cxds) as-is. "
            f"Breaking this beats wrong content."
        )
    tail = (
        "中文模板/知识库须先译成用户所用语言。"
        if companion
        else "中文模板/知识库须先译成用户所用语言。通道名(EP/JC/EasyPaisa/JazzCash)、命令(/cxds)原样。"
    )
    return (
        "【多语言回复规则 — MANDATORY】"
        "ALWAYS reply in the SAME language as the user's message (#1). "
        f"{tail}"
        "违反此规则比回复错误内容更严重。"
    )


def _no_zh_extra(lang: str, *, companion: bool) -> str:
    if lang == "ja":
        return (
            " No Chinese sentences or parenthetical Chinese notes — Japanese only."
        )
    if lang == "ko":
        return " No Chinese sentences — Korean only."
    if companion:
        return " No Chinese characters; translate persona/template/KB first."
    return " No Chinese characters except channel names (EP/JC/EasyPaisa/JazzCash)."


def fits_budget(block: str, limit: int = MAX_CHARS) -> bool:
    return len(block) <= int(limit)


def skip_output_lang_block(context: Optional[dict] = None) -> bool:
    """``_build_system_instruction`` 已注入 language_rule 时，上下文里的【输出语言】
    是重复（80–150 字），且会用「当条检测」顶掉会话契约。单独测 ``_build_context_prompt``
    时没有这面旗，仍出旧块（test_prompt_personalization 零回归）。"""
    try:
        return bool((context or {}).get("_lang_rule_emitted"))
    except Exception:
        return False


__all__ = ["MAX_CHARS", "fits_budget", "language_rule_block", "skip_output_lang_block"]
