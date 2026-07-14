# -*- coding: utf-8 -*-
"""
studio_advisor.py — 内容建议引擎

核心职责：
  1. 维护 20 种内容框架库（爆款结构模板）
  2. 根据 persona + 当日日期生成「今日内容建议」（无需任何 API Key）
  3. 输出 ContentBrief 结构体供 content_agent 使用
  4. 实现内容多样性：框架轮转 + 钩子类型轮转 + 情绪轮转

设计原则：
  - 零外部依赖（无 LLM Key 也可运行）
  - 确定性随机（日期 + persona_id 作为种子，保证同一天同一人设建议不变）
  - 可选 Serper 热词注入（有 SERPER_API_KEY 时自动增强）
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 20 种内容框架库（爆款结构）
# ---------------------------------------------------------------------------

CONTENT_FRAMEWORKS: List[Dict[str, Any]] = [
    {
        "id": "hook_question",
        "name": "问题钩子",
        "description": "用一个让人无法忽视的问题开场，引发观众好奇心",
        "structure": ["provocative_question", "problem_amplify", "solution_reveal", "proof", "cta"],
        "hook_template": "你{pain_point}吗？90%的人都做错了",
        "platform_fit": ["tiktok", "instagram", "xiaohongshu"],
        "virality_score": 9,
        "best_tone": "educational",
        "estimated_shares": "high",
    },
    {
        "id": "before_after",
        "name": "前后对比",
        "description": "展示转变过程，用对比制造戏剧张力",
        "structure": ["before_state", "turning_point", "after_state", "key_insight", "cta"],
        "hook_template": "我{timeframe}前还在{before_state}，现在{after_state}",
        "platform_fit": ["tiktok", "instagram"],
        "virality_score": 9,
        "best_tone": "inspiring",
        "estimated_shares": "very_high",
    },
    {
        "id": "myth_busting",
        "name": "误区揭秘",
        "description": "打破大众普遍认知，用反转制造记忆点",
        "structure": ["common_myth", "truth_reveal", "explanation", "examples", "cta"],
        "hook_template": "每个人都告诉你{common_advice}，但其实这是错的",
        "platform_fit": ["tiktok", "instagram", "linkedin", "twitter"],
        "virality_score": 10,
        "best_tone": "educational",
        "estimated_shares": "very_high",
    },
    {
        "id": "5_step_tutorial",
        "name": "五步教程",
        "description": "结构清晰的操作指南，给观众明确的行动路径",
        "structure": ["result_teaser", "step1", "step2", "step3", "step4", "step5", "cta"],
        "hook_template": "五步让你{desired_outcome}（第三步最关键）",
        "platform_fit": ["tiktok", "instagram", "linkedin", "xiaohongshu"],
        "virality_score": 8,
        "best_tone": "educational",
        "estimated_shares": "high",
    },
    {
        "id": "story_transformation",
        "name": "转变故事",
        "description": "第一人称真实故事，用情感共鸣建立信任",
        "structure": ["struggle_intro", "low_point", "discovery", "journey", "result", "invitation"],
        "hook_template": "一年前的我{struggle}，没想到今天{outcome}",
        "platform_fit": ["tiktok", "instagram", "facebook"],
        "virality_score": 9,
        "best_tone": "inspiring",
        "estimated_shares": "high",
    },
    {
        "id": "stat_shock",
        "name": "数据震撼",
        "description": "用令人惊讶的数据开场，建立权威感并引发思考",
        "structure": ["shocking_stat", "context", "implications", "solution", "cta"],
        "hook_template": "{percentage}%的人不知道{fact}，你是其中之一吗？",
        "platform_fit": ["linkedin", "twitter", "tiktok"],
        "virality_score": 8,
        "best_tone": "educational",
        "estimated_shares": "high",
    },
    {
        "id": "mistake_correction",
        "name": "避坑指南",
        "description": "列举常见错误，帮观众规避风险，建立专家形象",
        "structure": ["mistake_hook", "mistake1", "mistake2", "mistake3", "correct_approach", "cta"],
        "hook_template": "我犯过的{topic}三个致命错误，别让你也踩坑",
        "platform_fit": ["tiktok", "instagram", "xiaohongshu", "linkedin"],
        "virality_score": 9,
        "best_tone": "educational",
        "estimated_shares": "high",
    },
    {
        "id": "day_in_life",
        "name": "一天生活流水",
        "description": "真实感强的日常记录，打造亲近感和生活方式标签",
        "structure": ["morning_tease", "routine_1", "routine_2", "routine_3", "evening_reflection", "cta"],
        "hook_template": "我的{lifestyle_label}一天 — 从早上{time}开始",
        "platform_fit": ["tiktok", "instagram", "xiaohongshu"],
        "virality_score": 7,
        "best_tone": "casual",
        "estimated_shares": "medium",
    },
    {
        "id": "expert_3_tips",
        "name": "专家三招",
        "description": "压缩专业知识为3个可操作的技巧，高信息密度",
        "structure": ["credibility_hook", "tip1_title", "tip1_detail", "tip2_title", "tip2_detail", "tip3_title", "tip3_detail", "cta"],
        "hook_template": "做了{years}年{topic}，总结出这三个没人告诉你的秘诀",
        "platform_fit": ["linkedin", "tiktok", "instagram", "telegram"],
        "virality_score": 8,
        "best_tone": "educational",
        "estimated_shares": "high",
    },
    {
        "id": "mindset_shift",
        "name": "思维重构",
        "description": "挑战固有认知，提供全新视角，高分享率",
        "structure": ["limiting_belief", "reframe", "new_perspective", "real_world_example", "application", "cta"],
        "hook_template": "如果你还在用{old_mindset}思考{topic}，难怪{negative_result}",
        "platform_fit": ["tiktok", "instagram", "linkedin", "twitter"],
        "virality_score": 10,
        "best_tone": "inspiring",
        "estimated_shares": "very_high",
    },
    {
        "id": "comparison",
        "name": "AB对比测试",
        "description": "直观对比两种方案，帮助观众做决策，互动率高",
        "structure": ["comparison_setup", "option_a_detail", "option_b_detail", "verdict", "recommendation"],
        "hook_template": "{option_a} vs {option_b}：我实测了两个月，结果出乎意料",
        "platform_fit": ["tiktok", "instagram", "xiaohongshu"],
        "virality_score": 8,
        "best_tone": "casual",
        "estimated_shares": "high",
    },
    {
        "id": "behind_scenes",
        "name": "幕后揭秘",
        "description": "展示通常不为人知的过程，满足观众好奇心",
        "structure": ["teaser_hook", "process_reveal_1", "process_reveal_2", "unexpected_truth", "invitation"],
        "hook_template": "没人告诉你{topic}背后是怎么做到的，直到今天",
        "platform_fit": ["tiktok", "instagram", "xiaohongshu"],
        "virality_score": 9,
        "best_tone": "casual",
        "estimated_shares": "high",
    },
    {
        "id": "countdown",
        "name": "倒计时盘点",
        "description": "Top N 格式，节奏紧凑，完播率高",
        "structure": ["intro", "item5", "item4", "item3", "item2", "item1_highlight", "cta"],
        "hook_template": "改变我{topic}的五件事，第一名颠覆了我的认知",
        "platform_fit": ["tiktok", "instagram", "xiaohongshu", "telegram"],
        "virality_score": 8,
        "best_tone": "energetic",
        "estimated_shares": "high",
    },
    {
        "id": "success_formula",
        "name": "成功公式",
        "description": "结果前置 + 拆解路径，给观众可复制的方案",
        "structure": ["result_first", "formula_intro", "factor_1", "factor_2", "factor_3", "summary", "cta"],
        "hook_template": "我用这个公式{achievement}，只用了{timeframe}",
        "platform_fit": ["tiktok", "linkedin", "instagram"],
        "virality_score": 8,
        "best_tone": "inspiring",
        "estimated_shares": "high",
    },
    {
        "id": "emotional_hook",
        "name": "情感共鸣",
        "description": "直击痛点，用共情建立深层连接，评论互动率最高",
        "structure": ["relatable_pain", "empathy_statement", "hope_injection", "solution_path", "community_cta"],
        "hook_template": "如果你也{relatable_struggle}，这条视频是专门为你拍的",
        "platform_fit": ["tiktok", "instagram", "facebook"],
        "virality_score": 9,
        "best_tone": "emotional",
        "estimated_shares": "very_high",
    },
    {
        "id": "trending_reaction",
        "name": "热点借势",
        "description": "结合当前流行话题，借助热度快速起量",
        "structure": ["trend_reference", "personal_take", "unique_angle", "deeper_insight", "audience_poll"],
        "hook_template": "关于{trending_topic}，我有一个和所有人都不同的观点",
        "platform_fit": ["tiktok", "twitter", "instagram"],
        "virality_score": 10,
        "best_tone": "casual",
        "estimated_shares": "very_high",
    },
    {
        "id": "challenge_diy",
        "name": "挑战自己",
        "description": "设立挑战目标并记录过程，制造悬念和期待",
        "structure": ["challenge_setup", "rules", "day1_attempt", "struggle", "breakthrough", "final_result", "invite"],
        "hook_template": "我挑战{timeframe}{challenge_desc}，没想到发生了这些事",
        "platform_fit": ["tiktok", "instagram"],
        "virality_score": 8,
        "best_tone": "energetic",
        "estimated_shares": "high",
    },
    {
        "id": "q_and_a",
        "name": "问答互动",
        "description": "回应粉丝问题，强化社区归属感，评论引导效果好",
        "structure": ["question_intro", "answer_depth_1", "answer_depth_2", "bonus_insight", "next_question_cta"],
        "hook_template": "你们问了我{count}次关于{topic}的问题，今天统一回答",
        "platform_fit": ["tiktok", "instagram", "telegram", "xiaohongshu"],
        "virality_score": 7,
        "best_tone": "casual",
        "estimated_shares": "medium",
    },
    {
        "id": "social_proof",
        "name": "社会证明",
        "description": "展示他人成果，用真实案例消除疑虑，转化率最高",
        "structure": ["case_intro", "before_situation", "what_they_did", "results_achieved", "how_you_can_too", "cta"],
        "hook_template": "她用我分享的方法{achievement}，来看她是怎么做到的",
        "platform_fit": ["instagram", "tiktok", "telegram", "facebook"],
        "virality_score": 8,
        "best_tone": "inspiring",
        "estimated_shares": "high",
    },
    {
        "id": "quick_win",
        "name": "即刻见效",
        "description": "30秒内能执行的小技巧，即时满足感强，分享率高",
        "structure": ["promise_hook", "quick_tip", "why_it_works", "instant_result", "more_tips_cta"],
        "hook_template": "一个{topic}小技巧，30秒学会，今天就能用",
        "platform_fit": ["tiktok", "instagram", "xiaohongshu", "telegram"],
        "virality_score": 9,
        "best_tone": "energetic",
        "estimated_shares": "very_high",
    },
]

# 钩子类型轮转序列
HOOK_TYPES = ["question", "stat", "story", "contrast", "promise", "mystery"]

# 情绪/风格选项
TONE_OPTIONS = {
    "energetic":    {"label": "激励型", "emoji": "⚡", "desc": "让人立刻想行动"},
    "educational":  {"label": "教育型", "emoji": "📚", "desc": "干货实用，看完有收获"},
    "inspiring":    {"label": "励志型", "emoji": "🔥", "desc": "情感共鸣，激发改变"},
    "casual":       {"label": "轻松型", "emoji": "😄", "desc": "好玩，容易分享"},
    "emotional":    {"label": "情感型", "emoji": "💙", "desc": "深层连接，评论爆炸"},
}


# ---------------------------------------------------------------------------
# ContentBrief 数据结构
# ---------------------------------------------------------------------------

@dataclass
class ContentBrief:
    """
    内容生成方向说明书。
    从「每日建议」或「用户引导流程」生成，注入到 content_agent 中控制输出方向。
    """
    topic: str                          # 视频主题，如 "晨间五分钟健身"
    hook_type: str = "question"         # question / stat / story / contrast / promise / mystery
    tone: str = "energetic"            # energetic / educational / inspiring / casual / emotional
    audience: str = ""                  # 目标受众细化描述
    key_message: str = ""              # 核心信息（观众看完后记住的一句话）
    framework_id: str = "hook_question" # 内容框架 ID
    cta_direction: str = "community"    # community / offer / follow / engage
    variance_seed: int = 0             # 随机种子，保证同一 brief 每次生成有差异
    source: str = "advisor"            # advisor（系统建议）/ user（用户输入）
    # 可选增强字段
    trending_topic: str = ""           # Serper 热词（可选）
    competitor_angle: str = ""         # 竞品差异化角度（可选）
    avoid_topics: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ContentBrief":
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in d.items() if k in valid_keys})


# ---------------------------------------------------------------------------
# 建议生成引擎
# ---------------------------------------------------------------------------

class StudioAdvisor:
    """
    每日内容建议引擎。
    基于 persona 配置 + 当前日期生成3个差异化内容建议卡片。
    无需外部 API，零成本运行。
    """

    def __init__(self, serper_api_key: str = ""):
        self.serper_key = serper_api_key or os.environ.get("SERPER_API_KEY", "")

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def get_daily_suggestions(
        self,
        persona_config: Dict[str, Any],
        persona_id: str,
        n: int = 3,
        target_platforms: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        生成今日 n 条内容建议卡片。

        返回格式:
        [
          {
            "suggestion_id": "...",
            "title": "五分钟晨间健身挑战",
            "brief": ContentBrief.to_dict(),
            "framework": {...},
            "preview_hook": "你说没时间健身？那你有5分钟吗？",
            "preview_scenes": [...],
            "target_audience": "...",
            "estimated_virality": "high",
            "platform_fit": ["tiktok", "instagram"],
            "estimated_cost": 0.02,
            "why_today": "...",
          }
        ]
        """
        today = date.today().isoformat()
        seed = self._make_seed(persona_id, today)
        rng = random.Random(seed)

        themes = list(persona_config.get("content_themes", ["lifestyle", "tips", "motivation"]))
        niche = persona_config.get("niche", "lifestyle_fitness")
        language = persona_config.get("language", "english")
        platforms = target_platforms or list(persona_config.get("platform_strategy", {}).keys()) or ["tiktok"]

        # 今日趋势词（可选，有 Serper Key 时异步注入）
        trending = self._fetch_trending(niche, language) if self.serper_key else []

        # 读取框架历史性能，高通过率框架权重×2
        try:
            from .studio_db import get_framework_perf as _get_fw_perf
            perf_data = _get_fw_perf()
        except Exception:
            perf_data = {}

        def _fw_weight(fw_id: str) -> int:
            p = perf_data.get(fw_id, {})
            rate = p.get("approval_rate", 0.5)
            total = p.get("approved", 0) + p.get("rejected", 0)
            if total < 3:
                return 1  # 数据不足，权重相同
            return 2 if rate >= 0.7 else (1 if rate >= 0.4 else 0)  # 低于40%通过率暂停推荐

        # 选择今日框架（从全部框架里按种子选 n 个不重复）
        platform_fit_frameworks = [
            f for f in CONTENT_FRAMEWORKS
            if any(p in f.get("platform_fit", []) for p in platforms)
        ]
        if len(platform_fit_frameworks) < n:
            platform_fit_frameworks = CONTENT_FRAMEWORKS

        # 按性能数据加权扩展候选池
        candidates = []
        for fw in platform_fit_frameworks:
            w = _fw_weight(fw["id"])
            candidates.extend([fw] * w)  # 权重复制
        if not candidates:  # 全部被降权时用原始列表
            candidates = platform_fit_frameworks

        # 用种子确定今日框架池（不纯随机，保证一天内一致）
        shuffled = candidates[:]
        rng.shuffle(shuffled)
        # 去重（权重复制可能导致重复框架），保留顺序
        seen_ids = set()
        today_frameworks = []
        for fw in shuffled:
            if fw["id"] not in seen_ids:
                seen_ids.add(fw["id"])
                today_frameworks.append(fw)
            if len(today_frameworks) >= n:
                break

        suggestions = []
        for i, framework in enumerate(today_frameworks):
            # 每个建议用不同主题 + 不同钩子类型
            theme = themes[i % len(themes)]
            hook_type = HOOK_TYPES[(seed + i) % len(HOOK_TYPES)]
            tone = framework.get("best_tone", "energetic")

            # 结合趋势词（如果有）
            trend_topic = trending[i % len(trending)] if trending else ""

            brief = self._build_brief(
                persona_config=persona_config,
                theme=theme,
                framework=framework,
                hook_type=hook_type,
                tone=tone,
                trending_topic=trend_topic,
                variance_seed=seed + i * 31,
            )

            preview_hook = self._generate_preview_hook(brief, persona_config, framework)
            preview_scenes = self._generate_preview_scenes(brief, framework)
            audience_desc = self._describe_audience(persona_config, theme)

            suggestion = {
                "suggestion_id": f"{persona_id}-{today}-{i}",
                "index": i,
                "title": self._generate_title(theme, framework, persona_config),
                "brief": brief.to_dict(),
                "framework": {
                    "id": framework["id"],
                    "name": framework["name"],
                    "description": framework["description"],
                },
                "preview_hook": preview_hook,
                "preview_scenes": preview_scenes,
                "target_audience": audience_desc,
                "estimated_virality": self._estimate_virality(framework, hook_type),
                "platform_fit": [p for p in framework.get("platform_fit", []) if p in platforms],
                "estimated_cost": 0.02,  # slideshow default
                "tone_label": TONE_OPTIONS.get(tone, {}).get("label", tone),
                "tone_emoji": TONE_OPTIONS.get(tone, {}).get("emoji", "✨"),
                "hook_type_label": self._hook_type_label(hook_type),
                "why_today": self._generate_why_today(framework, theme, trend_topic),
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
            suggestions.append(suggestion)

        # 按预估病毒力排序
        virality_order = {"very_high": 0, "high": 1, "medium": 2}
        suggestions.sort(key=lambda s: virality_order.get(s["estimated_virality"], 99))
        return suggestions

    def build_brief_from_user_input(
        self,
        user_description: str,
        persona_config: Dict[str, Any],
        platform: str = "tiktok",
        tone: str = "energetic",
        framework_id: Optional[str] = None,
    ) -> ContentBrief:
        """
        将用户的自然语言描述转为 ContentBrief（无需 LLM）。
        通过关键词匹配选择最合适的框架和参数。
        """
        # 关键词 → 框架映射
        keyword_framework_map = {
            "教程": "5_step_tutorial", "步骤": "5_step_tutorial", "怎么": "5_step_tutorial",
            "错误": "mistake_correction", "避坑": "mistake_correction", "坑": "mistake_correction",
            "对比": "comparison", "vs": "comparison", "哪个好": "comparison",
            "故事": "story_transformation", "经历": "story_transformation", "转变": "before_after",
            "误区": "myth_busting", "真相": "myth_busting", "其实": "myth_busting",
            "秘诀": "expert_3_tips", "技巧": "expert_3_tips", "方法": "expert_3_tips",
            "挑战": "challenge_diy", "天": "challenge_diy",
            "一天": "day_in_life", "日常": "day_in_life",
            "快": "quick_win", "秒": "quick_win", "简单": "quick_win",
        }

        selected_fid = framework_id
        if not selected_fid:
            desc_lower = user_description.lower()
            for kw, fid in keyword_framework_map.items():
                if kw in desc_lower:
                    selected_fid = fid
                    break
            if not selected_fid:
                selected_fid = "hook_question"

        framework = next((f for f in CONTENT_FRAMEWORKS if f["id"] == selected_fid), CONTENT_FRAMEWORKS[0])

        # 推断钩子类型
        hook_type = "question"
        if any(w in user_description for w in ["数据", "研究", "%", "人"]):
            hook_type = "stat"
        elif any(w in user_description for w in ["故事", "经历", "那时"]):
            hook_type = "story"
        elif any(w in user_description for w in ["对比", "vs", "区别"]):
            hook_type = "contrast"

        return ContentBrief(
            topic=user_description[:100],
            hook_type=hook_type,
            tone=tone,
            audience=self._describe_audience(persona_config, user_description),
            key_message=user_description[:50],
            framework_id=selected_fid,
            variance_seed=int(datetime.now().timestamp()) % 10000,
            source="user",
        )

    def generate_storyboard(
        self,
        brief: ContentBrief,
        persona_config: Dict[str, Any],
        platform: str = "tiktok",
    ) -> Dict[str, Any]:
        """
        生成可视化故事板（脚本预览的核心数据）。
        这是「在花钱前让用户看到计划」的关键功能。
        """
        framework = next(
            (f for f in CONTENT_FRAMEWORKS if f["id"] == brief.framework_id),
            CONTENT_FRAMEWORKS[0]
        )
        niche = persona_config.get("niche", "lifestyle_fitness")
        cta_hook = persona_config.get("cta_hook", "Join our community!")
        language = persona_config.get("language", "english")

        # 平台参数
        platform_params = {
            "tiktok":     {"duration": 15, "shots": 6,  "ratio": "9:16"},
            "instagram":  {"duration": 15, "shots": 6,  "ratio": "9:16"},
            "telegram":   {"duration": 30, "shots": 8,  "ratio": "16:9"},
            "linkedin":   {"duration": 30, "shots": 6,  "ratio": "16:9"},
            "xiaohongshu":{"duration": 20, "shots": 8,  "ratio": "3:4"},
        }
        params = platform_params.get(platform, {"duration": 15, "shots": 6, "ratio": "9:16"})

        hook_line = self._generate_preview_hook(brief, persona_config, framework)

        scenes = []
        structure = framework.get("structure", ["hook", "body", "cta"])
        per_shot = round(params["duration"] / len(structure[:params["shots"]]), 1)

        scene_templates = {
            "hook": f"镜头：{self._scene_desc(brief.topic, 'opening', niche)}",
            "provocative_question": f"特写镜头：创作者面对镜头，眼神直视",
            "problem_amplify": f"场景：展示{brief.topic}相关的常见痛点画面",
            "solution_reveal": f"场景：展示解决方案，明亮积极的视觉风格",
            "proof": f"场景：成果展示，数字/截图/真实结果",
            "cta": f"结尾：创作者微笑，屏幕显示引流信息",
            "before_state": f"场景：展示改变前的状态，暗色调",
            "after_state": f"场景：展示改变后的状态，明亮对比",
            "turning_point": f"关键转折场景：强调改变的契机",
            "step1": f"步骤1画面：简洁示范动作/方法",
            "step2": f"步骤2画面：继续演示",
            "step3": f"步骤3画面：关键步骤强调",
            "step4": f"步骤4画面：深化",
            "step5": f"步骤5画面：完成效果",
        }

        for i, step in enumerate(structure[:params["shots"]]):
            t_start = round(i * per_shot, 1)
            t_end = round((i + 1) * per_shot, 1)
            is_hook = i == 0
            is_cta = i == len(structure) - 1

            narration = hook_line if is_hook else (cta_hook if is_cta else self._step_narration(step, brief, niche, language))
            scene_desc = scene_templates.get(step, f"场景{i+1}：{brief.topic}相关画面")
            visual_prompt = self._build_visual_prompt(step, brief, niche, is_hook, is_cta)

            scenes.append({
                "index": i + 1,
                "timestamp": f"{t_start:.1f}s - {t_end:.1f}s",
                "step_type": step,
                "scene_description": scene_desc,
                "narration": narration,
                "visual_prompt": visual_prompt,
                "is_hook": is_hook,
                "is_cta": is_cta,
                "editable": True,
            })

        full_script = "\n\n".join([
            f"[{s['timestamp']}]\n旁白：{s['narration']}\n画面：{s['scene_description']}"
            for s in scenes
        ])

        return {
            "storyboard": scenes,
            "full_script": full_script,
            "hook_line": hook_line,
            "cta_line": cta_hook,
            "platform": platform,
            "duration": params["duration"],
            "aspect_ratio": params["ratio"],
            "total_scenes": len(scenes),
            "framework_name": framework["name"],
            "estimated_engagement": framework.get("estimated_shares", "high"),
            "virality_score": framework.get("virality_score", 7),
            "brief": brief.to_dict(),
        }

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _make_seed(self, persona_id: str, date_str: str) -> int:
        h = hashlib.md5(f"{persona_id}:{date_str}".encode()).hexdigest()
        return int(h[:8], 16)

    def _build_brief(
        self,
        persona_config: Dict,
        theme: str,
        framework: Dict,
        hook_type: str,
        tone: str,
        trending_topic: str,
        variance_seed: int,
    ) -> ContentBrief:
        niche = persona_config.get("niche", "lifestyle")
        audience_raw = (
            f"{persona_config.get('target_gender', 'all')} "
            f"{persona_config.get('target_age', '25-35')} "
            f"interested in {niche.replace('_', ' ')}"
        )
        key_message = self._derive_key_message(theme, framework)

        return ContentBrief(
            topic=self._enrich_topic(theme, trending_topic, niche),
            hook_type=hook_type,
            tone=tone,
            audience=audience_raw,
            key_message=key_message,
            framework_id=framework["id"],
            cta_direction="community",
            variance_seed=variance_seed,
            trending_topic=trending_topic,
            source="advisor",
        )

    def _enrich_topic(self, theme: str, trending: str, niche: str) -> str:
        if trending:
            return f"{theme} (trending: {trending})"
        return f"{theme} for {niche.replace('_', ' ')}"

    def _derive_key_message(self, theme: str, framework: Dict) -> str:
        msgs = {
            "hook_question":       f"You already have everything you need to succeed with {theme}",
            "before_after":        f"Small consistent actions lead to dramatic transformation in {theme}",
            "myth_busting":        f"What most people believe about {theme} is completely wrong",
            "5_step_tutorial":     f"Anyone can master {theme} with the right step-by-step approach",
            "story_transformation":f"Real change in {theme} is possible for anyone who commits",
            "mindset_shift":       f"Your mindset about {theme} determines your results",
            "quick_win":           f"You can start improving your {theme} right now, today",
            "mistake_correction":  f"Avoiding common {theme} mistakes is the fastest path to success",
        }
        return msgs.get(framework["id"], f"Master {theme} with these proven strategies")

    def _generate_preview_hook(
        self,
        brief: ContentBrief,
        persona_config: Dict,
        framework: Dict,
    ) -> str:
        topic = brief.topic.split("(")[0].strip()
        niche = persona_config.get("niche", "lifestyle_fitness")
        language = persona_config.get("language", "english")

        hook_by_type = {
            "question": {
                "english": f"Are you struggling with {topic}? 90% of people get this wrong...",
                "italian": f"Hai difficoltà con {topic}? Il 90% delle persone sbaglia...",
                "portuguese": f"Você luta com {topic}? 90% das pessoas erram isso...",
                "arabic": f"هل تعاني من {topic}؟ 90٪ من الناس يخطئون في هذا...",
                "default": f"Struggling with {topic}? Most people completely miss this...",
            },
            "stat": {
                "english": f"Studies show 73% of people fail at {topic} — here's why",
                "default": f"73% of people fail at {topic} — here's the real reason",
            },
            "story": {
                "english": f"One year ago I couldn't imagine succeeding at {topic}. Today everything changed.",
                "default": f"One year ago I had zero results with {topic}. Here's what changed.",
            },
            "contrast": {
                "english": f"Everyone tells you to do X for {topic}. I did the opposite and here's what happened.",
                "default": f"They said {topic} was impossible. I proved them wrong.",
            },
            "promise": {
                "english": f"I'll show you exactly how to master {topic} in the next 60 seconds.",
                "default": f"Master {topic} in 60 seconds — watch this.",
            },
        }

        hook_map = hook_by_type.get(brief.hook_type, hook_by_type["question"])
        return hook_map.get(language, hook_map.get("english", hook_map.get("default", f"Let's talk about {topic}...")))

    def _generate_preview_scenes(self, brief: ContentBrief, framework: Dict) -> List[Dict]:
        structure = framework.get("structure", ["hook", "body", "cta"])[:4]
        return [
            {
                "step": s,
                "label": self._step_label(s),
                "narration_hint": self._step_narration(s, brief, "general", "english"),
            }
            for s in structure
        ]

    def _generate_title(self, theme: str, framework: Dict, persona_config: Dict) -> str:
        fw_name = framework["name"]
        niche_short = persona_config.get("niche", "lifestyle").split("_")[0]
        titles = [
            f"【{fw_name}】{theme}",
            f"{theme} — {fw_name}格式",
            f"今日推荐：{fw_name} × {theme}",
        ]
        return titles[hash(theme) % len(titles)]

    def _describe_audience(self, persona_config: Dict, theme: str) -> str:
        gender = persona_config.get("target_gender", "all")
        age = persona_config.get("target_age", "25-35")
        country = persona_config.get("country", "global")
        return f"{age}岁 {gender if gender != 'all' else '男女均可'} · {country} · 对{theme}感兴趣"

    def _estimate_virality(self, framework: Dict, hook_type: str) -> str:
        score = framework.get("virality_score", 7)
        if hook_type in ("question", "contrast", "mystery"):
            score += 1
        if score >= 9:
            return "very_high"
        if score >= 7:
            return "high"
        return "medium"

    def _hook_type_label(self, hook_type: str) -> str:
        labels = {
            "question": "疑问式", "stat": "数据式", "story": "故事式",
            "contrast": "对比式", "promise": "承诺式", "mystery": "悬念式",
        }
        return labels.get(hook_type, hook_type)

    def _generate_why_today(self, framework: Dict, theme: str, trending: str) -> str:
        base = f"「{framework['name']}」框架在{theme}领域历史完播率 +{framework.get('virality_score', 7) * 8}%"
        if trending:
            return base + f"，结合今日热词「{trending}」预计额外流量加成"
        return base

    def _scene_desc(self, topic: str, scene_type: str, niche: str) -> str:
        descs = {
            "opening": f"与{topic}相关的视觉冲击画面，高对比度，抓眼球",
            "tutorial": f"清晰展示{topic}操作步骤，稳定特写镜头",
            "result": f"展示{topic}的效果，明亮积极色调",
        }
        return descs.get(scene_type, f"{topic}相关场景")

    def _step_label(self, step: str) -> str:
        labels = {
            "hook": "开场钩子", "cta": "结尾引流", "provocative_question": "问题抛出",
            "problem_amplify": "问题深化", "solution_reveal": "解决方案", "proof": "效果证明",
            "before_state": "之前状态", "after_state": "之后状态", "turning_point": "转折点",
            "step1": "第一步", "step2": "第二步", "step3": "第三步", "step4": "第四步", "step5": "第五步",
        }
        return labels.get(step, step.replace("_", " ").title())

    def _step_narration(self, step: str, brief: ContentBrief, niche: str, language: str) -> str:
        topic = brief.topic.split("(")[0].strip()
        narrations = {
            "problem_amplify": f"Most people struggle with {topic} because they're missing one key piece...",
            "solution_reveal": f"The solution is actually simpler than you think. Here's what works:",
            "proof": f"And the results speak for themselves. Look at this...",
            "before_state": f"I used to be exactly where you are right now...",
            "after_state": f"But everything changed when I discovered this approach to {topic}.",
            "step1": f"Step 1: Start with the foundation — this is where most people skip ahead.",
            "step2": f"Step 2: Now build on that foundation with consistency.",
            "step3": f"Step 3: This is the game-changer that most tutorials leave out.",
            "cta": f"If this helped you, follow for more daily content like this!",
        }
        return narrations.get(step, f"Continuing with {topic}...")

    def _build_visual_prompt(self, step: str, brief: ContentBrief, niche: str, is_hook: bool, is_cta: bool) -> str:
        topic = brief.topic.split("(")[0].strip()
        style_map = {
            "lifestyle_fitness": "cinematic fitness photography, gym/outdoor setting, athletic wear",
            "beauty_fashion": "beauty editorial photography, soft lighting, cosmetics/fashion",
            "side_hustle_finance": "professional lifestyle, laptop/workspace, modern aesthetic",
            "business_success": "corporate professional setting, confident pose, business attire",
            "tech_career": "modern tech office, code on screen, young professional",
        }
        style = style_map.get(niche, "lifestyle photography, vibrant colors, social media optimized")

        if is_hook:
            return f"Eye-catching opening frame, {topic}, {style}, ultra HD, vertical 9:16, high contrast"
        if is_cta:
            return f"Warm closing shot, creator smiling, {style}, overlay text space, call-to-action feel"
        return f"{topic} in action, {style}, detailed demonstration, professional quality, vertical format"

    def _fetch_trending(self, niche: str, language: str) -> List[str]:
        """Serper API 获取热词（有 Key 时执行）。"""
        if not self.serper_key:
            return []
        try:
            import urllib.request
            query = f"{niche.replace('_', ' ')} trending {language} {date.today().strftime('%B %Y')}"
            url = "https://google.serper.dev/search"
            data = json.dumps({"q": query, "num": 5}).encode()
            req = urllib.request.Request(
                url, data=data,
                headers={"X-API-KEY": self.serper_key, "Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                result = json.loads(resp.read())
            topics = []
            for item in result.get("organic", [])[:3]:
                title = item.get("title", "")
                if title:
                    topics.append(title[:50])
            return topics
        except Exception as e:
            logger.debug("Serper fetch failed: %s", e)
            return []


# ---------------------------------------------------------------------------
# 模块级便捷函数
# ---------------------------------------------------------------------------

_advisor_instance: Optional[StudioAdvisor] = None


def get_advisor() -> StudioAdvisor:
    global _advisor_instance
    if _advisor_instance is None:
        serper_key = os.environ.get("SERPER_API_KEY", "")
        _advisor_instance = StudioAdvisor(serper_api_key=serper_key)
    return _advisor_instance


def get_daily_suggestions(
    persona_config: Dict[str, Any],
    persona_id: str,
    n: int = 3,
    target_platforms: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    # 懒加载先验数据（首次调用时注入行业基准，后续调用跳过）
    try:
        ensure_priors_initialized(persona_id)
    except Exception:
        pass
    return get_advisor().get_daily_suggestions(persona_config, persona_id, n, target_platforms)


def get_storyboard(
    brief_dict: Dict[str, Any],
    persona_config: Dict[str, Any],
    platform: str = "tiktok",
) -> Dict[str, Any]:
    brief = ContentBrief.from_dict(brief_dict)
    return get_advisor().generate_storyboard(brief, persona_config, platform)


def get_daily_suggestions_with_priors(
    persona_config: Dict[str, Any],
    persona_id: str,
    n: int = 3,
    target_platforms: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """get_daily_suggestions 的带先验懒加载版本（供路由层调用）。"""
    ensure_priors_initialized(persona_id)
    return get_advisor().get_daily_suggestions(persona_config, persona_id, n, target_platforms)


def get_frameworks() -> List[Dict[str, Any]]:
    """返回所有框架（供前端展示框架库）。"""
    virality_map = {10: "very_high", 9: "very_high", 8: "high", 7: "high", 6: "medium", 5: "medium"}
    return [
        {
            "id": f["id"],
            "name": f["name"],
            "name_zh": f["name"],          # 兼容前端 name_zh 字段
            "description": f["description"],
            "hook_template": f.get("hook_template", ""),
            "virality_score": virality_map.get(f["virality_score"], "medium"),
            "virality_raw": f["virality_score"],
            "platform_fit": f["platform_fit"],
            "estimated_shares": f["estimated_shares"],
            "best_tone": f["best_tone"],
        }
        for f in CONTENT_FRAMEWORKS
    ]


# ── 人设×框架先验矩阵（基于行业数据，冷启动用）────────────────────────────

PERSONA_FRAMEWORK_PRIORS: Dict[str, Dict[str, int]] = {
    # 框架ID → 虚拟通过次数（代表行业数据支持的先验强度）
    "italy_lifestyle":  {"before_after":5,"day_in_life":4,"emotional_hook":4,"social_proof":3,"quick_win":3,"story_transformation":3},
    "brazil_beauty":    {"before_after":6,"social_proof":5,"story_transformation":4,"emotional_hook":4,"day_in_life":3},
    "global_hustle":    {"success_formula":5,"expert_3_tips":5,"stat_shock":4,"mindset_shift":4,"myth_busting":3,"quick_win":3},
    "arabic_business":  {"expert_3_tips":5,"myth_busting":5,"stat_shock":4,"mindset_shift":4,"success_formula":3},
    "india_tech":       {"5_step_tutorial":6,"quick_win":5,"myth_busting":4,"expert_3_tips":4,"stat_shock":3},
    # 通用默认（未知人设使用）
    "_default":         {"hook_question":3,"before_after":3,"myth_busting":3,"quick_win":3,"expert_3_tips":2},
}


def inject_persona_priors(persona_id: str) -> int:
    """
    向 studio_framework_perf 注入指定人设的先验数据。
    只在该人设没有任何历史数据时执行（避免覆盖真实数据）。
    返回注入的框架数量。
    """
    priors = PERSONA_FRAMEWORK_PRIORS.get(persona_id) or PERSONA_FRAMEWORK_PRIORS.get("_default", {})
    if not priors:
        return 0

    try:
        from .studio_db import _get_conn, get_framework_perf
        existing = get_framework_perf()

        injected = 0
        with _get_conn() as conn:
            for fw_id, approved_count in priors.items():
                # 只在该框架完全没有数据时注入（不干扰真实积累的数据）
                if fw_id not in existing or (existing[fw_id]["approved"] + existing[fw_id]["rejected"]) == 0:
                    conn.execute("""
                        INSERT INTO studio_framework_perf(framework_id, approved_count)
                        VALUES (?, ?)
                        ON CONFLICT(framework_id) DO UPDATE SET
                            approved_count = MAX(approved_count, excluded.approved_count),
                            last_updated = datetime('now')
                    """, (fw_id, approved_count))
                    injected += 1
            conn.commit()

        logger.info("人设先验注入完成: persona=%s, 注入%d个框架", persona_id, injected)
        return injected
    except Exception as e:
        logger.warning("先验注入失败: %s", e)
        return 0


def ensure_priors_initialized(persona_id: str) -> None:
    """
    确保人设先验数据已初始化（在 get_daily_suggestions 中懒加载调用）。
    使用文件标记避免重复注入。
    """
    import os

    from src.host.device_registry import data_dir

    marker_dir = data_dir() / "studio"
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_dir / f".priors_{persona_id}.initialized"

    if not marker.exists():
        count = inject_persona_priors(persona_id)
        if count >= 0:
            marker.touch()
            logger.info("先验初始化标记已创建: %s", marker)
