# -*- coding: utf-8 -*-
"""人设发声链 —— 让群戏里每个号用**它自己那个人**的声音说话。

## 为什么需要这一层

``runtime.llm_generator`` 走的是 ``ai_client.chat(prompt)`` 这个**通用入口**：能出话，
但出的是「模型的默认语气」。多号同台时这是致命的——四个号一个味儿，句长、口癖、
emoji 密度、标点习惯全一样，人看不出是同一个模型，但**行为特征分析看得出**，这正是
群控最容易被抓的一类指纹。台词生成必须过该账号所绑人设。

第二件事更硬：``chat`` 通用入口**不过任何出站防线**。一句「作为一个AI助手，我建议…」
在 1:1 里只是尴尬，在群里是**当场社死**——所有群友都看见，且截图会传播。所以本模块
在出话后强制过一遍 ``persona_guard``。

## 为什么不复用 generate_inbox_draft / persona_reply

那两条链路是为 **1:1 收件箱**设计的：要 conversation row、chat_key、收件箱上下文、
会话记忆。群戏里这些**一个都没有**，硬凑就得编假数据——而 ECP 的账号隔离墙（见
``ecp`` 模块 docstring）恰恰规定「群戏上下文只能有群内公开事件」。为了复用而往里灌
伪造的会话对象，等于亲手在隔离墙上开洞，代价是角色在群里说出私聊内容。

所以这里走一条**轻量路径**：取人格描述 → 注入 ECP messages 最前 → ``chat`` →
``persona_guard`` 校验。只借人设的「声音」，不借收件箱的「上下文」。

## 为什么违规就宁可不发

守卫命中后只重试一次，第二次仍违规就返回空串让 runtime 跳过这一拍。理由是两边的
代价完全不对称：**少说一句话没人会注意**（真人群里本来就有人潜水），**说错一句话
会毁掉整个号乃至整场戏**。runtime 的 ``MAX_CONSECUTIVE_SKIPS`` 会在连续跳过时显式
报错，所以「静默跳过」不会退化成「静默演空场」。

## 降级原则

人设取不到、guard 模块不可用、manager 抛异常——一律**降级到当前行为**（无人设裸生成
/ 不校验）而不是中断。群戏的失败模式排序：说错话 > 不说话 > 少一层人设。
"""
from __future__ import annotations

import inspect
import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.companion.group_show.playbook import BeatDirective, CastMember
from src.companion.group_show.runtime import GenerateFn, flatten_messages

logger = logging.getLogger(__name__)

#: 守卫命中后的重试补充指令。刻意**不复述**违规原文（复述会把违规片段又喂回模型，
#: 反而提高复发率），只给出正向约束。
_RETRY_HINT = (
    "\n\n【重要修正】你刚才那句话暴露了非真人身份或带上了客服腔，群里没人这么说话。"
    "重说一遍：就是一个普通群友在随口插话，不要提 AI/助手/模型/系统，"
    "不要用「为您服务」「有什么可以帮您」这类客服用语。"
)


# ── 人设解析 ────────────────────────────────────────────────────────────────


def _resolve_manager(persona_manager: Any = None) -> Any:
    """拿到 PersonaManager；拿不到返回 None（＝本场戏无人设，裸生成）。"""
    if persona_manager is not None:
        return persona_manager
    try:
        from src.utils.persona_manager import PersonaManager
        return PersonaManager.get_instance()
    except Exception:  # noqa: BLE001 —— 人设层不可用不该让戏演不成
        logger.debug("[group_show] PersonaManager 不可用，本场按无人设生成",
                     exc_info=True)
        return None


def _desc_from_dict(persona: Dict[str, Any]) -> str:
    """manager 没有 ``format_persona_block`` 时的兜底：从人设字段手搓一段描述。

    只取「决定说话方式」的字段——名字/角色/性格/风格/口癖/禁语。群戏不需要
    身份硬锁那一大套（ECP 的 system 已经钉了「你就是群里的某某」）。
    """
    if not isinstance(persona, dict):
        return ""
    parts: List[str] = []
    name = str(persona.get("name") or "").strip()
    role = str(persona.get("role") or "").strip()
    if name or role:
        parts.append(f"你是{name}{('，' + role) if role else ''}。")
    p = persona.get("personality")
    if isinstance(p, dict):
        traits = [str(t).strip() for t in (p.get("traits") or []) if str(t).strip()]
        if traits:
            parts.append("性格：" + "、".join(traits) + "。")
        for key, label in (("style", "说话风格"), ("quirks", "口头禅")):
            val = str(p.get(key) or "").strip()
            if val:
                parts.append(f"{label}：{val}。")
    elif isinstance(p, str) and p.strip():
        parts.append(f"说话风格：{p.strip()}。")
    speaking = persona.get("speaking")
    if isinstance(speaking, dict):
        forbidden = [str(f).strip() for f in (speaking.get("forbidden_phrases") or [])
                     if str(f).strip()]
        if forbidden:
            parts.append("绝不说：" + "、".join(f"「{f}」" for f in forbidden[:8]) + "。")
    return "\n".join(parts)


def resolve_persona_voice(
    speaker: CastMember,
    persona_manager: Any = None,
) -> Tuple[Dict[str, Any], str]:
    """``speaker.persona_id`` → ``(人设 dict, 人格描述文本)``。

    取不到一律返回 ``({}, "")``＝无人设降级，**绝不抛**。

    刻意**只认显式注册的 profile**（``get_persona_by_id``）：直接调
    ``format_persona_block`` 会在 id 查不到时静默回落到域默认/兜底人设，那会让四个
    号又变回同一个味儿，还骗人说「已接人设」。宁可诚实地无人设。
    """
    pid = str(getattr(speaker, "persona_id", "") or "").strip()
    if not pid:
        return {}, ""
    pm = _resolve_manager(persona_manager)
    if pm is None:
        return {}, ""

    try:
        persona = pm.get_persona_by_id(pid)
    except Exception:  # noqa: BLE001
        logger.warning("[group_show] 人设查询失败 persona_id=%s", pid, exc_info=True)
        return {}, ""
    if not isinstance(persona, dict) or not persona:
        logger.info("[group_show] 人设 %s 未注册，本号按无人设生成", pid)
        return {}, ""

    # compact 档：核心声音特征 + 禁语，省 token 也少和 ECP 的群感知提示打架
    # （full 档带大量 1:1 私聊语境的硬约束，群戏用不上）。
    desc = ""
    try:
        desc = str(pm.format_persona_block(
            account_persona_id=pid, detail="compact", record_usage=False) or "").strip()
    except TypeError:
        # 旧签名（无 record_usage）兼容
        try:
            desc = str(pm.format_persona_block(
                account_persona_id=pid, detail="compact") or "").strip()
        except Exception:  # noqa: BLE001
            desc = ""
    except Exception:  # noqa: BLE001
        desc = ""
    if not desc:
        desc = _desc_from_dict(persona)
    return persona, desc


# ── 纯函数：身份锚点 ────────────────────────────────────────────────────────


def build_persona_preamble(persona_desc: str, speaker: Any) -> str:
    """人格描述 + 群内身份 → 一段身份锚点文本（无描述则返回空串）。

    两个刻意的处理：

    1. **群昵称优先于人设名**——人设里的名字（乃至「身份硬锁·必答此名」）是按 1:1
       写的，群里这个号顶着的是 ``display_name``。两者不一致时角色会自报另一个名字，
       群友一眼看出「号是批量配的」。这里显式声明以群昵称为准。
    2. **禁止复述人设**——把人格描述喂进去后，模型有概率把它当内容念出来
       （「我是个性格开朗的…」），那是最典型的 prompt 泄漏式暴露。
    """
    desc = str(persona_desc or "").strip()
    if not desc:
        return ""
    who = str(getattr(speaker, "display_name", "") or "").strip() \
        or str(getattr(speaker, "account_id", "") or "").strip()
    parts = [f"【你的人设·你说话就得是这个人的味道】\n{desc}"]
    if who:
        parts.append(
            f"你在这个群里的昵称是「{who}」——群友只认得这个名字，"
            "被问到叫什么就说这个，不要报上面人设里的其他名字。"
        )
    parts.append(
        "上面这段是你的性格设定，**只用来决定你怎么说话**（用词、句子长短、口癖、"
        "情绪浓度）；绝对不要把它念出来、复述出来或解释你的「设定/人设/角色」。"
    )
    return "\n\n".join(parts)


def inject_persona(
    messages: Sequence[Dict[str, str]],
    preamble: str,
) -> List[Dict[str, str]]:
    """把身份锚点插到 ECP messages **最前面**，返回新列表（不改原列表）。

    位置是有讲究的：ECP 的 system 里已经有群感知提示、本拍意图和禁止项，且它自己
    遵循「离生成越近权重越高」的排列。身份锚点是**底色**（我是谁、怎么说话），应该
    在最前；「这条要干什么、绝对不要什么」仍紧贴生成端。所以这里只加一层，
    **不重复拼任何群聊提示**。
    """
    out: List[Dict[str, str]] = [dict(m or {}) for m in (messages or [])]
    text = str(preamble or "").strip()
    if not text:
        return out
    return [{"role": "system", "content": text}] + out


# ── 出站守卫 ────────────────────────────────────────────────────────────────


def check_line_violations(text: str, persona: Optional[Dict[str, Any]] = None) -> List[str]:
    """台词的人设违规片段清单（空＝可发）。守卫不可用时返回空（降级放行）。

    比 1:1 链路多一条：**无论人设有没有开 ``identity.deny_ai``，都查「自曝 AI 身份」**。
    1:1 里那是人设配置项（有些客服号本来就承认是机器人），群戏里不是——群里冒出一句
    「作为AI助手」，暴露的是整批号，与这个号自己怎么配置无关。
    """
    body = str(text or "").strip()
    if not body:
        return []
    try:
        from src.utils.persona_guard import find_violations, matches_ai_self_identity
    except Exception:  # noqa: BLE001 —— 守卫缺席不阻断（与 1:1 链路同口径）
        logger.debug("[group_show] persona_guard 不可用，跳过台词校验", exc_info=True)
        return []
    hits: List[str] = []
    try:
        hits.extend(find_violations(body, persona or {}) or [])
        for hit in (matches_ai_self_identity(body) or []):
            if hit not in hits:
                hits.append(hit)
    except Exception:  # noqa: BLE001
        logger.debug("[group_show] 台词校验异常，按合规放行", exc_info=True)
        return []
    return hits


# ── 生成器 ──────────────────────────────────────────────────────────────────


async def _call_chat(ai_client: Any, prompt: str, temperature: float) -> str:
    """调 ``ai_client.chat``，兼容同步/异步实现与不认 ``strategy_overrides`` 的老签名。"""
    try:
        out = ai_client.chat(prompt, strategy_overrides={"temperature": temperature})
    except TypeError:
        out = ai_client.chat(prompt)
    if inspect.isawaitable(out):
        out = await out
    return str(out or "").strip()


def persona_generator(
    ai_client: Any,
    *,
    persona_manager: Any = None,
    guard: bool = True,
    temperature: float = 0.9,
) -> GenerateFn:
    """人设发声版台词生成器（符合 :data:`~...runtime.GenerateFn` 契约）。

    :param ai_client: 提供 ``chat(prompt, strategy_overrides=...)`` 的客户端。
    :param persona_manager: 缺省取 ``PersonaManager`` 单例；取不到＝无人设降级。
    :param guard: 关掉只在「排练时想看模型原始输出」的场景用；**真发链路别关**。
    :param temperature: 群聊闲聊需要高一点的随机性，默认 0.9（与 llm_generator 一致）。

    每一拍：人格描述 → 注入 ECP messages 最前 → ``chat`` → 守卫校验；
    违规重试一次，仍违规返回 ``""``（runtime 跳过这一拍）。任何异常都吞掉返回 ``""``。
    """

    async def _gen(speaker: CastMember, messages: List[Dict[str, str]],
                   directive: BeatDirective) -> str:
        beat_id = str(getattr(directive, "beat_id", "") or "")
        try:
            persona, desc = resolve_persona_voice(speaker, persona_manager)
            preamble = build_persona_preamble(desc, speaker)
            prompt = flatten_messages(inject_persona(messages, preamble))

            text = await _call_chat(ai_client, prompt, temperature)
            if not text or not guard:
                return text

            hits = check_line_violations(text, persona)
            if not hits:
                return text

            # 第一次违规：带正向修正指令重试一次。多数情况下模型这一次就规矩了。
            logger.warning(
                "[group_show] 台词命中人设违规 beat=%s persona=%s 片段=%r，重试一次",
                beat_id, getattr(speaker, "persona_id", ""), hits[:3])
            text = await _call_chat(ai_client, prompt + _RETRY_HINT, temperature)
            if not text:
                return ""
            hits = check_line_violations(text, persona)
            if not hits:
                return text

            # 两次都违规：这一拍不说话。少一句话没人注意，说错一句毁整场。
            logger.warning(
                "[group_show] 台词重试后仍违规 beat=%s persona=%s 片段=%r，本拍弃发",
                beat_id, getattr(speaker, "persona_id", ""), hits[:3])
            return ""
        except Exception as exc:  # noqa: BLE001 —— 单拍失败不该炸掉整场
            # 必须留痕：runtime 的连续失败检测靠这些空串判定「生成侧挂了」
            logger.warning("[group_show] 人设台词生成失败 beat=%s: %s", beat_id, exc)
            return ""

    return _gen


__all__ = [
    "build_persona_preamble",
    "check_line_violations",
    "inject_persona",
    "persona_generator",
    "resolve_persona_voice",
]
