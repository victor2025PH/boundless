"""``_goal_block`` 构建器（纯函数）——注入 LLM prompt 的「工作目标」上下文块。

铁律（营销安全边界，与 persona_guard / crisis safety net 同族）：
- **≤3 行、限长**：目标是「方向盘」不是「剧本」，绝不淹没人设与记忆上下文。
- **纪律行恒在**：自然融入 > 推进；对方情绪低落或明确拒绝 → 彻底放下目标只陪伴。
- **力度语义显式**：none=只字不提营销（纯陪伴）；soft=顺势自然带到；direct=可以直说
  （但依然禁止硬销/连环追问）。
- ``suppress_push``（同轮已有其他变现 directive，如 bazi 详批软引导）→ direct 降 soft，
  防同一条回复里双线推销。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

DEFAULT_MAX_CHARS = 360

_PUSH_LABEL = {
    "none": "今天只陪伴，营销内容只字不提",
    "soft": "只在话题自然贴近时轻轻带到，绝不生硬转折",
    "direct": "对方兴致好时可以直说，但禁止硬销、禁止连环追问",
}

_DISCIPLINE = (
    "【推进纪律】自然融入当下话题；对方情绪低落、敷衍或明确拒绝时，"
    "彻底放下目标只陪伴；绝不承诺线下见面/私下转账等越界内容。"
)


def build_goal_block(
    *,
    title: str,
    milestone_label: str,
    milestone_idx: int,
    day_index: int,
    total_days: int,
    intent: str = "",
    push_level: str = "soft",
    suppress_push: bool = False,
    milestone_total: int = 4,
    profile_gap: str = "",
    context_note: str = "",
    profile_facts: str = "",
    max_chars: int = DEFAULT_MAX_CHARS,
) -> Optional[str]:
    """组 3-6 行目标块。无标题（无目标）→ None。

    ``profile_gap``（可选）：摸底段的「画像缺口」提示（如「预算档、上线时间」），
    出一行轻量采集指令——只声明还想自然了解什么，严禁连环追问语义。
    ``context_note``（可选）：目标背景一行（params.note——挽回目标的「老客户
    曾购 X」/ 回流客户的「别当新客户从头摸底」这类战略语境；P7 前只在坐席 UI
    可见、从没进过 prompt）。
    ``profile_facts``（可选，P9a）：已采画像事实一行（「称呼:阿龙｜业务痛点:
    客服人手」——此前只驱动选品，LLM 看不到原值）。
    超长丢弃顺序：档案行（每轮 nice-to-have）→ 背景行 → 缺口行 → 截意图。
    """
    t = str(title or "").strip()
    if not t:
        return None
    lvl = str(push_level or "soft").strip().lower()
    if lvl not in _PUSH_LABEL:
        lvl = "soft"
    if suppress_push and lvl == "direct":
        lvl = "soft"

    mi_disp = max(0, int(milestone_idx)) + 1
    n_total = max(1, int(milestone_total or 4))
    head = f"【工作目标】{t} · 里程碑 {mi_disp}/{n_total} {milestone_label}"
    if total_days > 0:
        d = max(1, int(day_index))
        head += f"（第{min(d, int(total_days))}/{int(total_days)}天）"

    lines = [head]
    note = str(context_note or "").strip().replace("\n", " ")
    if len(note) > 80:
        note = note[:79] + "…"
    if note:
        lines.append(f"【背景】{note}")
    facts = str(profile_facts or "").strip().replace("\n", " ")
    if len(facts) > 96:
        facts = facts[:95] + "…"
    if facts:
        lines.append(f"【客户档案】{facts}")
    it = str(intent or "").strip()
    if it:
        lines.append(f"【今日意图】{it}（力度：{_PUSH_LABEL[lvl]}）")
    gap = str(profile_gap or "").strip()
    if gap:
        lines.append(
            f"【画像缺口】还想在闲聊中自然了解：{gap}"
            "（顺着话题带出来，一次最多问一件，绝不像查户口）")
    lines.append(_DISCIPLINE)
    block = "\n".join(lines)
    cap = max(120, int(max_chars or DEFAULT_MAX_CHARS))
    if len(block) > cap:
        # 超长丢弃顺序：客户档案行 → 背景行 → 画像缺口行 → 截意图行
        # （标题与纪律行是安全语义，不能丢；档案是每轮 nice-to-have——
        # 名字等事实通常也在记忆上下文里，最先让位）
        if facts and len(block) > cap:
            lines = [ln for ln in lines if not ln.startswith("【客户档案】")]
            block = "\n".join(lines)
        if note and len(block) > cap:
            lines = [ln for ln in lines if not ln.startswith("【背景】")]
            block = "\n".join(lines)
        if gap and len(block) > cap:
            lines = [ln for ln in lines if not ln.startswith("【画像缺口】")]
            block = "\n".join(lines)
        if it and len(block) > cap:
            idx = next((i for i, ln in enumerate(lines)
                        if ln.startswith("【今日意图】")), -1)
            if idx >= 0:
                overflow = len(block) - cap
                keep = max(12, len(it) - overflow - 1)
                lines[idx] = f"【今日意图】{it[:keep]}…"
                block = "\n".join(lines)
        block = block[:cap]
    return block


def goal_view_block(
    view: Dict[str, Any], *, suppress_push: bool = False,
    profile_gap: str = "",
    profile_facts: str = "",
    note_suffix: str = "",
    max_chars: int = DEFAULT_MAX_CHARS,
) -> Optional[str]:
    """从 service.goal_view 输出的视图字典组块（路由/注入共用的便捷口）。

    ``note_suffix``（P9b）：拼在 params.note 后的动态背景补充（流失原因
    应对策略「应对：聊性价比…」——采集常发生在目标创建**之后**，静态 note
    写死会错过；这里按当轮画像现值拼，无 note 时独立成背景行）。
    """
    if not isinstance(view, dict):
        return None
    beat = view.get("today") or {}
    milestones = view.get("milestones") or []
    params = view.get("params") or {}
    note = str(params.get("note") or "") if isinstance(params, dict) else ""
    sfx = str(note_suffix or "").strip()
    if sfx:
        note = f"{note}；{sfx}" if note else sfx
    return build_goal_block(
        title=str(view.get("title") or ""),
        milestone_label=str(view.get("milestone_label") or ""),
        milestone_idx=int(view.get("milestone_idx") or 0),
        day_index=int(view.get("day_index") or 1),
        total_days=int(view.get("total_days") or 0),
        intent=str((beat or {}).get("intent") or ""),
        push_level=str((beat or {}).get("push_level") or "soft"),
        suppress_push=suppress_push,
        milestone_total=len(milestones) if milestones else 4,
        profile_gap=profile_gap,
        context_note=note,
        profile_facts=profile_facts,
        max_chars=max_chars,
    )


__all__ = ["DEFAULT_MAX_CHARS", "build_goal_block", "goal_view_block"]
