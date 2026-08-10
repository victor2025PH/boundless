"""P37 — 智能下一步动作推荐引擎（情感陪伴场景）。

基于会话当前状态（风险信号 / 亲密度 / 流失风险 / 沉默时长 / 轮次等），
推荐最适合的下一步动作，帮助坐席快速决策。

场景聚焦：情感陪伴 / 聊天进阶（非电商）
  - 情感共鸣优先于产品推介
  - 进阶互动（亲密度提升）优先于关闭话题
  - 定期回访维系长期关系

动作类型（action_type）：
  template   — 发送预设话术模板
  task       — 创建坐席跟进任务
  tag        — 为会话打标签
  escalate   — 升级至人工/主管
  chain      — 触发工作链
  note       — 添加内部注解提醒
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# 情绪状态标签词表（单一事实源：__add_mood_tag 的 tag_options 与「当前情绪」读取
# 共用同一张表——P1-198 情绪标记状态化，路由据此从 conv_tags 反查当前生效情绪）。
#
# P1-198 续（2026-08-02）拆成两个正交维度（旧版四词互斥单选，标「需要关注」会静默
# 吞掉「情绪低落」——坐席丢信息而不自知）：
#   - emotion   组：客户情绪（人工标注 → effective_mood 仲裁 → AI 语气/主动节奏/让路）
#   - attention 组：跟进状态（纯工作流维度，不进 AI 仲裁）
# 组内互斥、跨组共存；MOOD_TAGS 保持四词合集（顺序不变）供旧消费方/门禁零改动。
MOOD_TAGS_EMOTION = ("情绪低落", "积极开朗")
MOOD_TAGS_ATTENTION = ("需要关注", "进展顺利")
MOOD_TAGS = MOOD_TAGS_EMOTION + MOOD_TAGS_ATTENTION

# 标签值（存储用中文 canonical，历史兼容）→ 展示层 i18n 键（路由用 tr() 出译文）。
MOOD_TAG_I18N = {
    "情绪低落": "inbox.nba.opt.mood_low",
    "积极开朗": "inbox.nba.opt.mood_positive",
    "需要关注": "inbox.nba.opt.attn_watch",
    "进展顺利": "inbox.nba.opt.attn_good",
}


def merge_mood_tag(existing: Optional[List[str]], tag: str) -> List[str]:
    """会话标签合并的**唯一入口**（execute-action 路由 / 工作链 runner / 批量打标共用）。

    旧病：互斥规则只在 execute-action 路由实现，工作链/批量打标裸 append →
    「情绪低落」「积极开朗」并存，「当前情绪」读数取决于数组顺序。收口为纯函数：
    - tag ∈ emotion 组 → 剔除组内旧值（情绪是单选状态）
    - tag ∈ attention 组 → 剔除组内旧值（跟进状态同理）
    - 其他标签 → 原语义（append 去重），零行为变化
    """
    tag = str(tag or "").strip()
    out = [str(t) for t in (existing or []) if str(t or "").strip()]
    if not tag:
        return out
    if tag in MOOD_TAGS_EMOTION:
        out = [t for t in out if t not in MOOD_TAGS_EMOTION]
    elif tag in MOOD_TAGS_ATTENTION:
        out = [t for t in out if t not in MOOD_TAGS_ATTENTION]
    if tag not in out:
        out.append(tag)
    return out


# ── 内置场景动作库（情感陪伴场景） ─────────────────────────────────────────

_BUILTIN_ACTIONS: List[Dict[str, Any]] = [
    {
        "action_id": "__empathy",
        "icon": "💝",
        "name": "情感共鸣回应",
        "action_type": "template",
        "builtin": True,
        "priority": 100,
        "config": {
            "hint": "表达理解与陪伴，避免说教，以倾听为主",
            "template_text": "我能理解你现在的感受，能多说说吗？我在这里陪着你。",
        },
        "trigger_conditions": ["sentiment_negative", "complaint", "churn_intent"],
    },
    {
        "action_id": "__deepen_topic",
        "icon": "🎯",
        "name": "深化话题引导",
        "action_type": "template",
        "builtin": True,
        "priority": 80,
        "config": {
            "hint": "对方聊得投入时，引导进入更深层的话题或分享",
            "template_text": "听你说这些，我很想多了解你。你平时最享受什么样的时光呢？",
        },
        "trigger_conditions": ["high_engagement", "long_conversation"],
    },
    {
        "action_id": "__advance_intimacy",
        "icon": "🌟",
        "name": "进阶互动建议",
        "action_type": "template",
        "builtin": True,
        "priority": 75,
        "config": {
            "hint": "对话轮次多、关系稳定后，适时升温互动",
            "template_text": "和你聊天总有很多收获，我们可以更多分享彼此的生活吗？",
        },
        "trigger_conditions": ["intimacy_growing", "many_turns"],
    },
    {
        "action_id": "__special_care",
        "icon": "🎁",
        "name": "特别关怀问候",
        "action_type": "template",
        "builtin": True,
        "priority": 70,
        "config": {
            "hint": "对方沉默一段时间后，主动发起温暖问候",
            "template_text": "最近没有见到你，想知道你还好吗？希望你一切都顺心。",
        },
        "trigger_conditions": ["silent_3d", "silent_7d"],
    },
    {
        "action_id": "__schedule_followup",
        "icon": "📅",
        "name": "创建回访任务",
        "action_type": "task",
        "builtin": True,
        "priority": 65,
        "config": {
            "hint": "会话即将结束时，安排下一次联系时间",
            "due_hours": 72,
            "note": "定期回访，维持情感连接",
        },
        "trigger_conditions": ["conversation_closing", "silent_3d"],
    },
    {
        "action_id": "__add_mood_tag",
        "icon": "🏷",
        # 名称显式说明标的是「客户」的状态（P1-198 客户实测困惑：分不清标客户还是标 AI）；
        # hint 与 tag_groups 的展示文案由路由按请求语言经 tr() 重写（见 workflow routes），
        # 这里是 zh 缺省值。旧 hint「便于后续个性化」是未兑现的承诺，已按真实行为改写。
        "name": "标记客户情绪状态",
        "action_type": "tag",
        "builtin": True,
        "priority": 55,
        "config": {
            "hint": "给客户当前状态打标签：情绪标注会在时效窗内引导 AI 语气与主动节奏",
            "tag_options": list(MOOD_TAGS),
            "tag_groups": [
                {"key": "emotion", "options": list(MOOD_TAGS_EMOTION)},
                {"key": "attention", "options": list(MOOD_TAGS_ATTENTION)},
            ],
        },
        "trigger_conditions": ["any"],
    },
    {
        "action_id": "__human_escalate",
        "icon": "🔴",
        "name": "升级人工接管",
        "action_type": "escalate",
        "builtin": True,
        "priority": 120,   # 最高优先级
        "config": {
            "hint": "情绪极度负面或对话陷入危机时，立即转人工",
            "reason": "高风险情绪干预",
        },
        "trigger_conditions": ["crisis_signal", "escalation_intent", "churn_intent_high"],
    },
    # 「添加内部备注」2026-08-02 上午曾因桌面壳 Electron 不支持 window.prompt()
    # （点击静默无反应）整卡删除；同日下午组件改为卡内 inline 输入框
    # （cp-next-actions.js notebox，两端可用）后恢复——病因在输入方式，不在能力。
    {
        "action_id": "__add_internal_note",
        "icon": "📝",
        "name": "添加内部备注",
        "action_type": "note",
        "builtin": True,
        "priority": 40,
        "config": {
            "hint": "记录关键信息供团队共享",
        },
        "trigger_conditions": ["any"],
    },
]

# ── 场景检测规则 ──────────────────────────────────────────────────────────────

_CRISIS_KW = ["不想活了", "活着没意思", "想消失", "轻生", "自杀",
              "don't want to live", "no reason to live", "end it all"]

_NEGATIVE_KW = ["难过", "伤心", "孤独", "寂寞", "绝望", "痛苦", "迷茫",
                "sad", "lonely", "hopeless", "depressed", "hurt"]


class NextActionRecommender:
    """P37：情感陪伴场景下一步动作推荐器。"""

    def recommend(
        self,
        *,
        risk_signals: Optional[List[Dict[str, Any]]] = None,
        last_msg_text: str = "",
        last_msg_direction: str = "in",
        message_count: int = 0,
        silence_hours: float = 0.0,
        churn_risk_level: str = "",
        qa_score: int = -1,
        custom_actions: Optional[List[Dict[str, Any]]] = None,
        limit: int = 5,
        followup_task_enabled: bool = True,
    ) -> List[Dict[str, Any]]:
        """推荐最适合的下一步动作（内置 + 自定义合并）。

        followup_task_enabled=False（config ``inbox.next_actions.follow_up_task``）
        时不再推荐「创建回访任务」——P1-198：客户反馈该动作与工作目标计划语义重叠，
        且无 contact_id 时点了什么都建不了，允许部署级关闭。

        Returns:
            [{action_id, name, icon, action_type, config, reason, priority}]
            按 priority 降序，最多返回 limit 条
        """
        risk_signals = risk_signals or []
        custom_actions = custom_actions or []

        # 检测当前会话信号
        signals = self._detect_signals(
            risk_signals=risk_signals,
            last_msg_text=last_msg_text,
            last_msg_direction=last_msg_direction,
            message_count=message_count,
            silence_hours=silence_hours,
            churn_risk_level=churn_risk_level,
        )

        # 内置动作评分
        candidates: List[Dict[str, Any]] = []
        for act in _BUILTIN_ACTIONS:
            if not followup_task_enabled and act["action_id"] == "__schedule_followup":
                continue
            matched = self._match_triggers(act["trigger_conditions"], signals)
            if matched:
                candidates.append({
                    **act,
                    "reason": self._build_reason(matched, signals),
                    "matched_signals": matched,
                    "_score": act["priority"] + len(matched) * 5,
                })

        # 自定义动作评分
        for act in custom_actions:
            if not act.get("enabled", True):
                continue
            triggers = act.get("trigger_conditions") or ["any"]
            if isinstance(triggers, str):
                try:
                    import json as _j
                    triggers = _j.loads(triggers)
                except Exception:
                    triggers = [triggers]
            matched = self._match_triggers(triggers, signals)
            if matched or "any" in triggers:
                candidates.append({
                    **act,
                    "reason": f"自定义动作：{act.get('name', '')}",
                    "matched_signals": matched,
                    "_score": int(act.get("sort_order") or 0) + len(matched) * 5,
                })

        # 排序并截断
        candidates.sort(key=lambda x: x.get("_score", 0), reverse=True)
        # 清理内部排序字段
        for c in candidates:
            c.pop("_score", None)
            c.pop("trigger_conditions", None)
            c.pop("matched_signals", None)

        return candidates[:limit]

    # ── 信号检测 ────────────────────────────────────────────────────────────

    def _detect_signals(
        self,
        *,
        risk_signals: List[Dict[str, Any]],
        last_msg_text: str,
        last_msg_direction: str,
        message_count: int,
        silence_hours: float,
        churn_risk_level: str,
    ) -> List[str]:
        """把各维度输入转换为统一信号标签列表。"""
        sigs: List[str] = []
        text_lc = last_msg_text.lower() if last_msg_text else ""

        # 危机信号（最高优先级）
        if any(kw in text_lc for kw in _CRISIS_KW):
            sigs.append("crisis_signal")

        # 情绪负面
        if any(kw in text_lc for kw in _NEGATIVE_KW):
            sigs.append("sentiment_negative")

        # 外部传入的风险信号
        for rs in risk_signals:
            sigs.append(rs.get("signal", ""))

        # 流失风险
        if churn_risk_level == "high":
            sigs.append("churn_intent_high")
        elif churn_risk_level == "medium":
            sigs.append("churn_intent")

        # 沉默时段
        if silence_hours >= 168:     # 7 天
            sigs.append("silent_7d")
        elif silence_hours >= 72:    # 3 天
            sigs.append("silent_3d")

        # 轮次相关
        if message_count >= 20:
            sigs.append("long_conversation")
            sigs.append("many_turns")
        if message_count >= 8:
            sigs.append("high_engagement")

        # 进阶互动条件
        if message_count >= 10 and churn_risk_level not in ("high",):
            sigs.append("intimacy_growing")

        # 末条为出站（坐席刚回）
        if last_msg_direction in ("out", "outbound"):
            sigs.append("conversation_closing")

        # 通配符
        sigs.append("any")
        return list(dict.fromkeys(sigs))  # 去重保序

    @staticmethod
    def _match_triggers(trigger_conditions: List[str], signals: List[str]) -> List[str]:
        """返回命中的信号列表（空列表=未命中）。"""
        if "any" in trigger_conditions:
            return ["any"]
        return [t for t in trigger_conditions if t in signals]

    @staticmethod
    def _build_reason(matched: List[str], signals: List[str]) -> str:
        _REASON_MAP = {
            "crisis_signal":       "⚠ 检测到危机信号，建议立即人工介入",
            "sentiment_negative":  "情绪偏负面，建议先共情",
            "complaint":           "用户有投诉情绪",
            "churn_intent":        "有流失倾向信号",
            "churn_intent_high":   "高流失风险，需主动挽留",
            "escalation_intent":   "对话有升级趋势",
            "silent_7d":           "沉默超 7 天，关系维护关键期",
            "silent_3d":           "沉默超 3 天，适合主动问候",
            "long_conversation":   "对话轮次充足，关系稳定",
            "high_engagement":     "对话活跃，互动良好",
            "intimacy_growing":    "亲密度成长阶段，适合深化",
            "many_turns":          "多轮深入交流",
            "conversation_closing":"对话即将结束",
            "any":                 "通用动作，适用于任何场景",
        }
        return "；".join(_REASON_MAP.get(m, m) for m in matched if m in _REASON_MAP)
