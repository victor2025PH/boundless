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
    max_chars: int = DEFAULT_MAX_CHARS,
) -> Optional[str]:
    """组 3 行目标块。无标题（无目标）→ None。"""
    t = str(title or "").strip()
    if not t:
        return None
    lvl = str(push_level or "soft").strip().lower()
    if lvl not in _PUSH_LABEL:
        lvl = "soft"
    if suppress_push and lvl == "direct":
        lvl = "soft"

    mi_disp = max(0, int(milestone_idx)) + 1
    head = f"【工作目标】{t} · 里程碑 {mi_disp}/4 {milestone_label}"
    if total_days > 0:
        d = max(1, int(day_index))
        head += f"（第{min(d, int(total_days))}/{int(total_days)}天）"

    lines = [head]
    it = str(intent or "").strip()
    if it:
        lines.append(f"【今日意图】{it}（力度：{_PUSH_LABEL[lvl]}）")
    lines.append(_DISCIPLINE)
    block = "\n".join(lines)
    cap = max(120, int(max_chars or DEFAULT_MAX_CHARS))
    if len(block) > cap:
        # 超长优先截意图行（标题与纪律行是安全语义，不能丢）
        if it:
            overflow = len(block) - cap
            keep = max(12, len(it) - overflow - 1)
            lines[1] = f"【今日意图】{it[:keep]}…"
            block = "\n".join(lines)
        block = block[:cap]
    return block


def goal_view_block(
    view: Dict[str, Any], *, suppress_push: bool = False,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> Optional[str]:
    """从 service.goal_view 输出的视图字典组块（路由/注入共用的便捷口）。"""
    if not isinstance(view, dict):
        return None
    beat = view.get("today") or {}
    return build_goal_block(
        title=str(view.get("title") or ""),
        milestone_label=str(view.get("milestone_label") or ""),
        milestone_idx=int(view.get("milestone_idx") or 0),
        day_index=int(view.get("day_index") or 1),
        total_days=int(view.get("total_days") or 0),
        intent=str((beat or {}).get("intent") or ""),
        push_level=str((beat or {}).get("push_level") or "soft"),
        suppress_push=suppress_push,
        max_chars=max_chars,
    )


__all__ = ["DEFAULT_MAX_CHARS", "build_goal_block", "goal_view_block"]
