"""记忆事实的视角转换（确定性纯函数，零 IO）。

抽取层落库的事实以运营视角书写（主语「客户」——见 ai_client 记忆抽取 prompt），
但注入生成 prompt 时，对话的另一端**就是**这位「客户」。不转换视角就把
「客户的妈妈在装修」原样塞进英文人设的开场指令，模型会直译成
*your client's mom*（#341 实锤）——对方从来没有「客户」。

这里统一把第三人称的运营称谓改写成 prompt 自身的约定称谓「TA」（proactive
prompt 里「关于TA的这些事」「TA 之前说的」一直用 TA 指对方），并给 prompt 一行
显式说明，让「TA＝正在聊的这个人」在生成侧成为硬约束而不是靠模型猜。
"""

from __future__ import annotations

import re
from typing import Iterable, List

# 运营视角的对方称谓（中文）。「用户」「对方」在事实里同样指对话另一端。
_ZH_SUBJECT_RE = re.compile(
    r"(该|这位|这个|此)?(客户|客戶|顾客|用户|對方|对方)(?!名|服务|服務|经理|端|群)")
# 英文事实（少数抽取器直出英文时）——只吃「the customer/client/user」整词形态。
_EN_SUBJECT_RE = re.compile(
    r"\b(?:the\s+)?(?:customer|client|user)(?P<pos>'s|’s)?\b", re.IGNORECASE)

PERSPECTIVE_NOTE = (
    "（称谓约定：下面提到的「TA」「对方」都指你此刻正在聊的这个人本人。"
    "绝不要把TA称作「客户」「你的客户」或 your client / your customer——"
    "TA 不是谁的客户，就是你在和TA说话。）\n"
)


def to_second_person(fact: str) -> str:
    """把事实里的运营称谓（客户/用户/对方…）改写为对话约定称谓「TA」。

    - 「客户的妈妈在装修」→「TA的妈妈在装修」
    - 「该客户有一个女儿」→「TA有一个女儿」
    - "the customer's mom is renovating" → "TA's mom is renovating"
    幂等；空串/非字符串原样返回。
    """
    s = str(fact or "")
    if not s:
        return s
    s = _ZH_SUBJECT_RE.sub("TA", s)

    def _en(m: "re.Match[str]") -> str:
        return "TA's" if m.group("pos") else "TA"

    s = _EN_SUBJECT_RE.sub(_en, s)
    return s


def facts_to_second_person(facts: Iterable[str]) -> List[str]:
    """批量转换，保序去重、去空。"""
    out: List[str] = []
    for f in facts or []:
        t = to_second_person(str(f or "").strip())
        if t and t not in out:
            out.append(t)
    return out


__all__ = ["PERSPECTIVE_NOTE", "to_second_person", "facts_to_second_person"]
