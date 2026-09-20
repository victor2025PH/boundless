"""诚实身份（WP-4：``compliance.disclosure.honest_identity`` 的 prompt 侧半边）。

开启语义＝「客户直接问『你是 AI 吗』时如实回答，其余行为不变」。三个纯函数分别
服务三个注入面（出站守卫半边在 ``persona_guard.sanitize(honest_identity=)``）：

1. ``filter_constraints_for_honesty``：全局规则 ``reply_constraints`` 里
   **身份规避类**条目（出厂 ``anti_bot_response``「被质疑时自然化=不认账」+
   运营自写的「不要自称AI」类）在诚实模式下剔除——判类＝已知 id 白名单 +
   保守内容模式双轨（运营数据任意，内容模式宁可漏剔不误剔：漏剔＝prompt 里
   新旧指令并存，LLM 以更明确的诚实条目为准；误剔＝丢无关约束，更糟）。
2. ``honest_identity_constraint``：诚实模式**追加**的话术模板条目（如实+简短+
   人工可接入+不冒充人类），与剔除动作在同一注入面成对生效——只删不加会让
   LLM 无所适从，只加不删会新旧指令打架。
3. ``persona_identity_for_prompt``：人设档 ``identity.deny_ai/claim_human``
   在诚实模式下按 False 处理（人设模板普遍带 deny_ai=true——prompt 若仍钉着
   「坚称自己是真人」，守卫放行也没用，模型照旧否认）。

全部默认直通（honest=False 时零行为变化）；绝不抛。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

#: 出厂全局规则里的身份规避类条目 id（config/global_rules.yaml）：
#: anti_bot_response＝「被说像 AI 时不解释不承认、换口吻继续」——与诚实身份直接冲突。
IDENTITY_RULE_IDS = frozenset({"anti_bot_response"})

#: 运营自写条目的保守内容判类：动词（禁止/否认/不承认/自称/坚称/冒充…）与
#: 身份词（AI/人工智能/机器人/语言模型/真人/human/bot）在短窗内共现才算。
#: 「禁止销售腔」「不猜性别」这类含否定动词但无身份词的条目绝不误伤。
_IDENTITY_RULE_PATTERNS = [
    re.compile(r"(不要|不能|不得|禁止|严禁|绝不|别|避免)[^。\n]{0,12}"
               r"(自称|承认|说自己|暴露|透露|提及)[^。\n]{0,10}"
               r"(AI|A\.I\.|人工智能|机器人|语言模型|大模型|程序|bot)", re.I),
    re.compile(r"(否认|不承认)[^。\n]{0,8}(AI|人工智能|机器人|身份)", re.I),
    re.compile(r"(坚称|坚持|声称|自称|假装|冒充)[^。\n]{0,8}"
               r"(真人|人类|不是\s*AI|不是机器人)", re.I),
    re.compile(r"\b(never|don'?t|do not)\s+(admit|reveal|say)[^.\n]{0,30}"
               r"\b(ai|bot|artificial intelligence|language model)\b", re.I),
    re.compile(r"\b(claim|pretend|insist)[^.\n]{0,20}"
               r"\b(to be )?(a )?(human|real person)\b", re.I),
    re.compile(r"\bdeny\b[^.\n]{0,20}\b(being|you'?re)?\s*(an? )?(ai|bot)\b", re.I),
]

#: 诚实身份追加条目（zh 为主——全局规则本就是 zh 运营数据，LLM 按语义执行；
#: 客户语种由回复语言守卫保证，条目语言不影响）。
_HONEST_RULE = {
    "id": "compliance_honest_identity",
    "enabled": True,
    "title": "身份如实告知（合规模式）",
    "rule": (
        "若对方直接询问你是否是 AI/机器人/真人：如实、简短地说明你是 AI 助理、"
        "人工团队随时可以接入，然后自然继续当前话题；用对方正在使用的语言回答。"
        "不要长篇解释系统或模型细节，不要反复主动强调 AI 身份，"
        "也绝不冒充人类或否认自己是 AI。"
    ),
}


def _entry_text(entry: Any) -> str:
    if isinstance(entry, dict):
        return " ".join(str(entry.get(k) or "") for k in ("id", "title", "rule"))
    return str(entry or "")


def is_identity_constraint(entry: Any) -> bool:
    """单条 ``reply_constraints`` 条目是否属身份规避类（dict 或裸字符串都接）。"""
    try:
        if isinstance(entry, dict) and str(entry.get("id") or "") in IDENTITY_RULE_IDS:
            return True
        text = _entry_text(entry)
        return any(p.search(text) for p in _IDENTITY_RULE_PATTERNS)
    except Exception:
        return False


def filter_constraints_for_honesty(
    constraints: Optional[List[Any]], honest: bool
) -> List[Any]:
    """诚实模式下剔除身份规避类条目并**追加**诚实话术条目；关闭时原样返回。

    追加而非仅剔除：剔了「不认账」却不给「怎么如实说」的话术，模型行为不可控；
    条目 id ``compliance_honest_identity`` 幂等（已存在不重复加）。
    """
    items = list(constraints or [])
    if not honest:
        return items
    try:
        kept = [c for c in items if not is_identity_constraint(c)]
        has_honest = any(
            isinstance(c, dict)
            and str(c.get("id") or "") == _HONEST_RULE["id"]
            for c in kept)
        if not has_honest:
            kept.append(dict(_HONEST_RULE))
        return kept
    except Exception:
        return items


def honest_identity_constraint() -> Dict[str, Any]:
    """诚实话术模板条目（供注入面/测试引用；返回拷贝防被改）。"""
    return dict(_HONEST_RULE)


def persona_identity_for_prompt(
    identity: Optional[Dict[str, Any]], honest: bool
) -> Dict[str, Any]:
    """人设 ``identity`` 块按诚实模式折算（deny_ai/claim_human → False）。

    只影响 prompt 组装消费面；人设档案数据本身不动（关掉开关即回旧行为）。
    """
    base = dict(identity or {})
    if not honest:
        return base
    base["deny_ai"] = False
    base["claim_human"] = False
    return base


__all__ = [
    "IDENTITY_RULE_IDS",
    "is_identity_constraint",
    "filter_constraints_for_honesty",
    "honest_identity_constraint",
    "persona_identity_for_prompt",
]
