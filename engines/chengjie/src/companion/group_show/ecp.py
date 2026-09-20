# -*- coding: utf-8 -*-
"""ECP 自我视角投影（Egocentric Context Projection）—— 群戏反串味的核心防线。

**要解决的事故**：多个 AI 号在同一个群里演戏时，最省事的做法是把「上帝视角」的群历史
（谁在第几秒说了什么，包括我们自己所有号的台词）原样喂给每个角色的 LLM。这么干必然出两类
线上事故：

1. **echoing（复读）**：角色看见一段没有角色归属的连续文本，会把别人刚说过的话再说一遍，
   或者顺着别人的口吻续写——群里出现两个人说一模一样的话，一眼假。
2. **persona drift（人格漂移 / 串味）**：角色分不清「哪句是我说的」，于是把别的号的立场、
   语气、甚至身份接过来——种草的号突然开始质疑自己，质疑的号突然开始卖货。

**ECP 的解法**：不给上帝视角，只给**当前发言者自己的视角**。同一份群历史，对每个号投影出
一份不同的 messages：

- 它自己说过的话 → ``role="assistant"``（模型天然认得「这是我之前的输出」）；
- **其他任何人**说的话 → ``role="user"``，并在内容前显式标注发言人显示名。

第二条是本模块最容易被「优化」掉、也最不能动的语义：**我们的其他号和真实群友，在当前
speaker 眼里必须完全一视同仁**。角色不知道、也不该知道群里还有同伙——一旦它知道，它就会
配合演戏（互相捧哏、抢答、称呼对方昵称），那正是群控最典型的识别特征。

──────────────────────────────────────────────────────────────────────────────
**安全不变量（账号隔离墙，捅破就是串味事故）**

本模块**只接受群内公开事件流**（:class:`ShowEvent` 序列，即群里所有人都看得见的消息），
**绝不接受、也绝不注入任何 1:1 私聊记忆**：

- 函数签名上就没有任何私聊/记忆参数——想传都传不进来（这是刻意的设计，不是遗漏）；
- 输出 messages 的内容严格来自「传入的群事件 + 导演指令 + 群感知提示」三者，
  不会凭空多出第四种来源；
- 调用方**不得**把 episodic memory / 私聊上下文伪装成 ``ShowEvent`` 塞进 ``history``。

违反它的后果是角色在群里说出「你上次私聊跟我说……」——对被点名的用户是隐私事故，对其余
群友是「这几个号互相认识」的铁证。:data:`~...playbook.DEFAULT_MUST_NOT` 里那条
「不要提及或编造你和某个群成员的私聊经历」是最后一道文本层兜底，本模块是结构层的第一道。
──────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Sequence

from src.companion.group_show.playbook import (
    BeatDirective,
    CastMember,
    ShowEvent,
)

# ── 常量 ────────────────────────────────────────────────────────────────────

#: 他人发言的显示名前缀标签——刻意不区分「我们的号」与「真人」，见模块 docstring
SPEAKER_PREFIX_LABEL = "群友"

#: 显示名解析不出来时的兜底称呼（宁可含糊，也不要把 account_id 之外的东西编出来）
UNKNOWN_SPEAKER_NAME = "某群友"

#: 控制类事件（导演让路 / 收尾），群里根本没有这条消息，不进上下文
_CONTROL_KINDS = frozenset({"yield", "terminate"})

#: 默认最多回溯的群消息条数（防 prompt 撑爆；群聊上下文本来就该短）
DEFAULT_MAX_TURNS = 20

#: 软广强度（0..10）→ 给 LLM 的口径提示。分档而非线性，是因为 LLM 对「7 分力度」无感，
#: 对「可以明确推荐但别用广告腔」有感。
_SOFT_AD_HINTS = (
    (0, "这条纯闲聊，完全不要提任何产品、工具或服务。"),
    (3, "最多顺口带一句，不要展开、不要说好处，听起来不能像在推荐东西。"),
    (6, "可以自然提到自己在用的东西，但要像分享自己的经验，不要罗列卖点。"),
    (10, "可以明确说这个东西好用，但仍然是群友聊天的口吻——不要广告腔，不要发链接。"),
)


# ── 小工具 ──────────────────────────────────────────────────────────────────


def format_speaker_line(name: str, text: str) -> str:
    """把他人的一句群发言格式化成带发言人标注的一行。

    形如 ``[群友 陈美玲] 这个会不会封号啊``。前缀存在的意义是让 LLM 在同一个
    ``user`` 角色里也能分清「这句是谁说的」——没有它，多人群聊会退化成一坨匿名文本，
    模型就会开始复读和认领别人的立场。
    """
    display = str(name or "").strip() or UNKNOWN_SPEAKER_NAME
    body = str(text or "").strip()
    return f"[{SPEAKER_PREFIX_LABEL} {display}] {body}"


def _resolve_name(
    account_id: str,
    name_resolver: Optional[Callable[[str], str]],
) -> str:
    """account_id → 显示名；无解析器 / 解析失败 / 解析为空 → 回落 account_id 本身。

    解析器是调用方注入的（通常查账号表），它挂了不该让整场戏停摆——群里显示一个
    account_id 只是难看，比抛异常中断发言好得多。
    """
    raw = str(account_id or "").strip()
    if name_resolver is None:
        return raw
    try:
        resolved = str(name_resolver(raw) or "").strip()
    except Exception:  # noqa: BLE001 —— 名字解析失败绝不能拖垮发言链路
        return raw
    return resolved or raw


def _soft_ad_hint(soft_level: int) -> str:
    try:
        level = int(soft_level)
    except (TypeError, ValueError):
        level = 5
    level = max(0, min(10, level))
    for ceiling, hint in _SOFT_AD_HINTS:
        if level <= ceiling:
            return hint
    return _SOFT_AD_HINTS[-1][1]


def _is_visible(ev: ShowEvent) -> bool:
    """该事件在群里是否真的「被人看见过」。

    ``yield``/``terminate`` 是导演的内部控制事件，群里并不存在这条消息；空文本同理
    （比如无配文的物料）——把它们塞进上下文只会让模型以为有人发了空话。
    """
    kind = str(getattr(ev, "kind", "") or "").strip().lower()
    if kind in _CONTROL_KINDS:
        return False
    return bool(str(getattr(ev, "text", "") or "").strip())


def _build_system_prompt(
    speaker: CastMember,
    directive: BeatDirective,
    group_hint: str,
) -> str:
    """拼这一拍的 system 指令：我是谁 → 群里什么情况 → 这条要干什么 → 绝对不要什么。

    顺序是有讲究的：身份锚点放最前（ECP 的第二重保险，防止长历史把人格冲淡），
    禁止项放最后（离生成最近，权重最高）。
    """
    parts: List[str] = []

    # 身份锚点：明确「除我之外都是普通群友」，堵死角色认出同伙互相捧哏的可能
    who = str(speaker.display_name or "").strip() or str(speaker.account_id or "").strip()
    parts.append(
        f"【你是谁】你就是群里的「{who}」，一个普通群成员。下面的对话记录里，"
        f"标着 [{SPEAKER_PREFIX_LABEL} xxx] 的都是别的群友，你和他们只是同一个群里的陌生人，"
        "你不了解他们的底细，也不要表现出你们认识或事先商量过。"
    )

    hint = str(group_hint or "").strip()
    if hint:
        parts.append(f"【群里的情况】{hint}")

    intent = str(getattr(directive, "intent", "") or "").strip()
    if intent:
        parts.append(
            f"【你这条想表达什么】{intent}\n"
            "注意这只是意图，不是台词——用你自己的说话习惯临场说出来，别照抄这句话。"
        )

    product = str(getattr(directive, "product", "") or "").strip()
    media = str(getattr(directive, "media", "") or "").strip()
    if product:
        parts.append(f"【这条涉及的东西】{product}")
    if media:
        parts.append(f"【这条会配一份材料】{media}（你的话要能接得上它，别自己描述图里内容）")
    parts.append(f"【分寸】{_soft_ad_hint(getattr(directive, 'soft_level', 5))}")

    # 真人插话优先级最高：这时候还念剧本就是当着真人的面演戏，最容易被察觉
    human = str(getattr(directive, "respond_to_human", "") or "").strip()
    if human:
        parts.append(
            "【有真人在群里说话，先回应他】刚刚有群友说：「" + human + "」。\n"
            "你这条要先真实地回应这句话本身，把你原本想说的往后放或者干脆不说——"
            "真人问了却没人正经理他，比不说话更假。"
        )

    must_not = tuple(getattr(directive, "must_not", ()) or ())
    if must_not:
        banned = "\n".join(f"- {str(x).strip()}" for x in must_not if str(x).strip())
        if banned:
            parts.append("【绝对不要】\n" + banned)

    return "\n\n".join(parts)


# ── 主函数 ──────────────────────────────────────────────────────────────────


def project_history_for(
    speaker: CastMember,
    history: Sequence[ShowEvent],
    *,
    directive: BeatDirective,
    group_hint: str = "",
    name_resolver: Optional[Callable[[str], str]] = None,
    max_turns: int = DEFAULT_MAX_TURNS,
) -> List[Dict[str, str]]:
    """把群历史投影成 ``speaker`` 自己的视角，产出可直接送 LLM 的 chat messages。

    :param speaker: 当前这一拍要发言的演员（账号 × 人设）。
    :param history: **群内公开事件流**（含我们的号与真实群友），按发生顺序。
    :param directive: 导演给本拍的意图指令（只有意图，没有台词）。
    :param group_hint: 群感知提示（群名/群氛围/正在聊什么），拼进 system。
    :param name_resolver: ``account_id -> 显示名``；缺省或解析失败时回落 account_id。
    :param max_turns: 只保留最近 N 条**可见**群消息；``<= 0`` 表示不带历史（只给 system）。
    :returns: ``[{"role": "system"|"user"|"assistant", "content": str}, ...]``，
        首条恒为 system，其后按时间顺序排列。

    投影规则（本函数的全部灵魂）：

    - ``ev.speaker_account == speaker.account_id`` → ``assistant``，内容即原文，
      **不加任何前缀**（它自己的话就是它的话）；
    - 其他所有人（我们的别的号 **和** 真实群友，一视同仁）→ ``user``，内容经
      :func:`format_speaker_line` 加上 ``[群友 名字]`` 前缀。

    .. warning::
       **安全不变量**：本函数只接受群内公开事件流，**绝不接受也绝不注入任何 1:1 私聊
       记忆**。签名上没有任何记忆/私聊入口，输出内容严格来自「``history`` 里的群事件 +
       ``directive`` + ``group_hint``」，不存在第四种来源。这是多号共底座的账号隔离墙，
       捅破即串味事故（角色会在群里说出别人的私聊内容）。调用方也不得把私聊记录伪装成
       :class:`ShowEvent` 塞进 ``history``。
    """
    messages: List[Dict[str, str]] = [
        {"role": "system", "content": _build_system_prompt(speaker, directive, group_hint)}
    ]

    try:
        limit = int(max_turns)
    except (TypeError, ValueError):
        limit = DEFAULT_MAX_TURNS
    if limit <= 0:
        return messages

    visible = [ev for ev in (history or []) if _is_visible(ev)]
    # 截断在过滤**之后**做：否则一串控制事件会把真正的对话挤出窗口
    recent = visible[-limit:]

    me = str(getattr(speaker, "account_id", "") or "").strip()
    for ev in recent:
        text = str(ev.text or "").strip()
        who = str(getattr(ev, "speaker_account", "") or "").strip()
        if me and who == me:
            messages.append({"role": "assistant", "content": text})
        else:
            name = _resolve_name(who, name_resolver)
            messages.append({"role": "user", "content": format_speaker_line(name, text)})
    return messages


__all__ = [
    "SPEAKER_PREFIX_LABEL",
    "UNKNOWN_SPEAKER_NAME",
    "DEFAULT_MAX_TURNS",
    "format_speaker_line",
    "project_history_for",
]
