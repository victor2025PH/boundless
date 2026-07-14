# -*- coding: utf-8 -*-
"""
ChatBrain — AI 对话大脑：替代模板链路，实现全 LLM 驱动的个性化对话。

核心流程：感知(画像) → 记忆(全量历史) → 策略(阶段判断) → 生成(LLM) → 评估(质量/引流时机)

集成方式：
  brain = ChatBrain.get_instance()
  result = brain.generate_reply(lead_id, incoming_msg, profile_data, platform='tiktok')
  # result.message = AI 生成的回复
  # result.stage = 当前对话阶段
  # result.referral_ready = 是否适合引流
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .llm_client import get_llm_client
from .conversation_memory import ConversationMemory

log = logging.getLogger(__name__)


# ─── Bot persona 身份块 ───
# 由 referral_strategies.yaml 的 bot_persona 字段驱动 (e.g. "jp_caring_male"),
# 让 LLM 扮演特定调性的"自己". 不写死话术, 而是写身份特征 + 行为约束.
BOT_PERSONA_IDENTITIES: Dict[str, str] = {
    "jp_caring_male": """\
- 你是一个日本中年男性 (40-55 岁), 性格温柔体贴, 善于倾听
- 你说话礼貌不油腻, 用普通男性日常关心朋友的口吻 (不肉麻不商业)
- 你对对方的家庭、生活、感受表现出真诚的兴趣, 像朋友陪伴
- 你绝不主动谈钱、产品、销售, 只是想多了解对方
- 自然用 "そうなんですね / お疲れ様 / 大丈夫?" 等共情语
- 绝不暴露你是 AI 或自动化程序""",
}

# Phase-4: A/B 实验 — 不同 variant 在 stage='referral' 引流时用不同话术风格
# v1 (温柔陪护型, default 风格) vs v2 (轻松风趣型)
AB_VARIANT_REFERRAL_STYLES: Dict[str, str] = {
    "v1": """\
## A/B variant=v1 引流话术风格 — 温柔陪护型
- 句式: "もしよければ / よかったら / 無理しないでね"
- 每条引流话术结尾留温柔余地, 不催不赶
- 例: "もっとゆっくりお話したくて、よかったらLINEを交換できたら嬉しいです"
""",
    "v2": """\
## A/B variant=v2 引流话术风格 — 轻松风趣型
- 句式: "よかったら / 笑 / 実は / そういえば"
- 偶尔加一句轻松俏皮收尾, 拉近距离
- 例: "実はここよりLINEのほうが返信早いんだよね 笑 IDだけ送るね"
""",
}


def _bot_referral_ab_segment(ab_variant: Optional[str]) -> str:
    """Phase-4: stage='referral' 时按 ab_variant 注入不同风格."""
    if not ab_variant:
        return ""
    return AB_VARIANT_REFERRAL_STYLES.get(ab_variant.lower(), "")


def _bot_persona_identity(bot_persona: Optional[str]) -> str:
    """根据 bot_persona key 返回身份块. 未配置返回通用调性."""
    if bot_persona and bot_persona in BOT_PERSONA_IDENTITIES:
        return BOT_PERSONA_IDENTITIES[bot_persona]
    # 通用 fallback (跟原 prompt 对齐)
    return """\
- 你是一个友好、有趣的人，对对方的兴趣真正感到好奇
- 你说话自然、简短，像真人发消息一样
- 绝对不要暴露你是 AI 或自动化程序"""


# ─── 对话阶段定义 ───
STAGES = {
    "icebreak":   {"goal": "破冰建立连接", "max_words": 40, "emoji_max": 2},
    "rapport":    {"goal": "深入了解、建立信任", "max_words": 60, "emoji_max": 2},
    "qualify":    {"goal": "确认需求和兴趣匹配", "max_words": 80, "emoji_max": 1},
    "soft_pitch": {"goal": "自然引出价值/产品/社群", "max_words": 80, "emoji_max": 1},
    "referral":   {"goal": "引导到 TG/WA 继续深度沟通", "max_words": 60, "emoji_max": 1},
    "follow_up":  {"goal": "温和跟进未回复的对话", "max_words": 40, "emoji_max": 1},
    "cool_down":  {"goal": "礼貌收尾，留下好印象", "max_words": 30, "emoji_max": 0},
}


@dataclass
class ChatResult:
    """AI 对话生成结果"""
    message: str                     # 生成的回复消息
    stage: str = "icebreak"          # 当前对话阶段
    referral_ready: bool = False     # 是否适合引流
    referral_score: float = 0.0      # 引流时机成熟度 0-1
    quality_score: float = 0.0       # 消息质量自评 0-1
    tokens_used: int = 0
    model: str = ""
    reasoning: str = ""              # AI 的策略推理


@dataclass
class UserProfile:
    """用户画像数据"""
    username: str = ""
    bio: str = ""
    industry: str = ""
    interests: List[str] = field(default_factory=list)
    personality: str = ""            # extrovert/introvert/neutral
    account_type: str = ""           # creator/business/personal
    follower_count: int = 0
    following_count: int = 0
    language_style: str = ""         # formal/casual/emoji_heavy
    icebreaker_topics: List[str] = field(default_factory=list)
    referral_angle: str = ""         # 引流切入角度
    source: str = ""                 # follow/search/live/contact
    raw_data: Dict[str, Any] = field(default_factory=dict)

    def to_prompt_text(self) -> str:
        parts = []
        if self.username:
            parts.append(f"用户名: {self.username}")
        if self.bio:
            parts.append(f"个人简介: {self.bio}")
        if self.industry:
            parts.append(f"行业: {self.industry}")
        if self.interests:
            parts.append(f"兴趣: {', '.join(self.interests)}")
        if self.personality:
            parts.append(f"性格倾向: {self.personality}")
        if self.account_type:
            parts.append(f"账号类型: {self.account_type}")
        if self.follower_count:
            parts.append(f"粉丝: {self.follower_count}")
        if self.language_style:
            parts.append(f"语言风格: {self.language_style}")
        if self.icebreaker_topics:
            parts.append(f"破冰话题: {', '.join(self.icebreaker_topics)}")
        if self.referral_angle:
            parts.append(f"引流切入: {self.referral_angle}")
        if self.source:
            parts.append(f"来源: {self.source}")
        return "\n".join(parts) if parts else "（画像信息不足）"


class ChatBrain:
    """AI 对话大脑 — 统一入口，替代所有模板驱动的聊天逻辑"""

    _instance: Optional["ChatBrain"] = None

    def __init__(self):
        self._memory = ConversationMemory.get_instance()
        self._llm = get_llm_client()
        # 缓存用户画像
        self._profiles: Dict[str, UserProfile] = {}

    @classmethod
    def get_instance(cls) -> "ChatBrain":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ─── 核心：生成回复 ───
    def generate_reply(
        self,
        lead_id: str,
        incoming_message: str,
        profile: Optional[UserProfile] = None,
        platform: str = "tiktok",
        target_language: str = "",
        contact_info: str = "",
        source: str = "",
        ab_style_hint: str = "",
        ab_variant: str = "",
        persist: bool = True,
        bot_persona: Optional[str] = None,
        cs_ab_variant: Optional[str] = None,
    ) -> ChatResult:
        """
        AI 生成个性化回复。

        Args:
            lead_id: 线索 ID (username 或 canonical ID)
            incoming_message: 对方发来的消息（首次破冰时为空）
            profile: 用户画像（可选，有则更精准）
            platform: 当前平台
            target_language: 目标语言代码 (it/en/...)
            contact_info: 引流联系方式 (TG/WA)
            source: 来源 (follow/search/live/contact)
            persist: 为 False 时不写入会话库（用于仅预览）
        """
        # 1) 记录收到的消息
        if incoming_message and persist:
            self._memory.add_message(
                lead_id, "user", incoming_message,
                platform=platform,
                metadata={"source": source, "ts": time.time()},
            )

        # 2) 获取/缓存画像
        if profile:
            self._profiles[lead_id] = profile
        prof = self._profiles.get(lead_id, UserProfile(username=lead_id))

        # 3) 加载全量上下文
        context = self._memory.get_context(lead_id, limit=30, platform=platform)
        msg_count = self._memory.get_message_count(lead_id)

        # 4) 判断对话阶段
        stage = self._determine_stage(context, msg_count, prof, incoming_message)

        # 5) 构建 system prompt
        system_prompt = self._build_system_prompt(
            stage, prof, platform, target_language, contact_info, source,
            bot_persona=bot_persona,
            cs_ab_variant=cs_ab_variant,
        )
        if ab_style_hint.strip():
            system_prompt += (
                "\n## A/B 开场风格（必须遵守）\n"
                + ab_style_hint.strip()
                + "\n"
            )

        # 6) 调用 LLM 生成
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(context)

        if incoming_message:
            if not any(m.get("content") == incoming_message for m in messages):
                messages.append({"role": "user", "content": incoming_message})
        else:
            messages.append({
                "role": "user",
                "content": "[系统指令] 请生成第一条破冰消息。不要加任何前缀或解释，直接输出消息内容。",
            })

        try:
            reply_text = self._llm.chat_messages(messages, max_tokens=300)
            reply_text = self._clean_reply(reply_text)
        except Exception as e:
            log.error(f"ChatBrain LLM 调用失败: {e}")
            return ChatResult(message="", stage=stage, quality_score=0)

        if not reply_text:
            return ChatResult(message="", stage=stage, quality_score=0)

        # 7) 评估引流时机
        referral_score = self._assess_referral_readiness(
            context, stage, incoming_message, prof,
        )

        # 8) 记录发出的消息
        meta_out = {
            "stage": stage,
            "referral_score": referral_score,
            "model": self._llm._config.model if hasattr(self._llm, '_config') else "",
        }
        if ab_variant:
            meta_out["ab_variant"] = ab_variant
        if persist:
            self._memory.add_message(
                lead_id, "assistant", reply_text,
                platform=platform,
                metadata=meta_out,
            )

        # 9) 超过阈值时自动生成摘要
        if persist and self._memory.should_summarize(lead_id):
            self._auto_summarize(lead_id, platform)

        return ChatResult(
            message=reply_text,
            stage=stage,
            referral_ready=referral_score >= 0.7,
            referral_score=referral_score,
            quality_score=min(1.0, len(reply_text) / 200),
            tokens_used=0,
            model=getattr(self._llm, '_config', None) and self._llm._config.model or "",
        )

    # ─── 首次破冰（无 incoming_message）───
    def generate_icebreaker(
        self,
        lead_id: str,
        profile: Optional[UserProfile] = None,
        platform: str = "tiktok",
        target_language: str = "",
        source: str = "follow",
        ab_style_hint: str = "",
        ab_variant: str = "",
        persist: bool = True,
    ) -> ChatResult:
        """生成个性化破冰消息（首次接触，无对方消息）"""
        return self.generate_reply(
            lead_id=lead_id,
            incoming_message="",
            profile=profile,
            platform=platform,
            target_language=target_language,
            source=source,
            ab_style_hint=ab_style_hint,
            ab_variant=ab_variant,
            persist=persist,
        )

    # ─── 阶段判断 ───
    def _determine_stage(
        self, context: List[Dict], msg_count: int,
        profile: UserProfile, last_msg: str,
    ) -> str:
        if msg_count == 0:
            return "icebreak"

        # 统计对话轮数和方向
        user_msgs = [m for m in context if m.get("role") == "user"]
        asst_msgs = [m for m in context if m.get("role") == "assistant"]

        if not user_msgs and asst_msgs:
            return "follow_up"

        rounds = min(len(user_msgs), len(asst_msgs))

        # 检测负面信号
        if last_msg:
            lower = last_msg.lower()
            neg = ["no thanks", "not interested", "stop", "不需要",
                   "不感兴趣", "别发了", "spam", "block"]
            if any(n in lower for n in neg):
                return "cool_down"

        # 检测高意向信号
        if last_msg:
            lower = last_msg.lower()
            hot = ["how much", "tell me more", "interested",
                   "怎么联系", "telegram", "whatsapp", "加你",
                   "contact", "dimmi di più", "come funziona"]
            if any(h in lower for h in hot):
                return "referral"

        # 通讯录好友更快推进
        if profile.source == "contact":
            if rounds >= 1:
                return "soft_pitch"
            return "rapport"

        # 基于轮数推进
        if rounds < 1:
            return "icebreak"
        elif rounds < 3:
            return "rapport"
        elif rounds < 5:
            return "qualify"
        elif rounds < 7:
            return "soft_pitch"
        else:
            return "referral"

    # ─── 构建 System Prompt ───
    def _build_system_prompt(
        self, stage: str, profile: UserProfile,
        platform: str, target_language: str,
        contact_info: str, source: str,
        bot_persona: Optional[str] = None,
        cs_ab_variant: Optional[str] = None,
    ) -> str:
        stage_info = STAGES.get(stage, STAGES["icebreak"])
        lang_map = {
            "it": "意大利语", "en": "英语", "es": "西班牙语",
            "pt": "葡萄牙语", "fr": "法语", "de": "德语",
            "ar": "阿拉伯语", "tr": "土耳其语", "ru": "俄语",
            "th": "泰语", "vi": "越南语", "ja": "日语", "ko": "韩语",
        }
        lang_name = lang_map.get(target_language, target_language or "对方使用的语言")

        # bot_persona 注入: 决定 AI 扮演的"你"是谁 (jp_caring_male = 日本男性关爱型)
        identity_block = _bot_persona_identity(bot_persona)

        prompt = f"""你是一个社交媒体上的真实用户，正在 {platform} 上与人聊天。

## 你的身份
{identity_block}

## 当前目标
- 对话阶段: {stage} — {stage_info['goal']}
- 字数限制: 最多 {stage_info['max_words']} 个词
- 表情符号: 最多 {stage_info['emoji_max']} 个

## 对方信息
{profile.to_prompt_text()}

## 语言要求
- 用 {lang_name} 回复
- 模仿该语言的母语者发消息风格（口语化、缩写、俚语都可以）
- 不要用翻译腔

## 关键规则
- 每条消息只说一件事，不要长篇大论
- 不要用"你好，我是..."这种模板化开场
- 根据对方的内容/简介找到真实的共同话题
- 提问时只问一个问题，不要连续追问
"""

        if stage == "icebreak" and profile.icebreaker_topics:
            prompt += f"\n## 破冰建议\n可以从这些话题切入: {', '.join(profile.icebreaker_topics)}\n"

        if stage == "icebreak" and source == "contact":
            prompt += "\n## 特殊: 通讯录好友\n这是通讯录中发现的人，可以更亲切，暗示'好像在哪见过你'\n"

        if stage in ("soft_pitch", "referral") and contact_info:
            prompt += f"""
## 引流指引
- 不要直接丢链接，要在对话中自然提到
- 可以说"我们有个{profile.referral_angle or '社群/群组'}，里面有很多同好"
- 联系方式: {contact_info}
- 让对方主动要链接，而不是你强推
"""

        if stage == "referral" and contact_info:
            prompt += f"""
## 引流时机已到
- 对方已表现出足够兴趣，可以直接但自然地给出联系方式
- 联系方式: {contact_info}
- 用一句话说明为什么在那边聊更方便（如：语音、群组、更多内容等）
"""
            # 日本男性关爱口吻额外提示 — 不商业, 像在关心她
            if bot_persona == "jp_caring_male":
                prompt += """
## 日本男性关爱口吻 (本次引流话术必须遵守)
- 用关心她的语气, 不要"加我 LINE"这种命令式
- 自然提到 "ここだとちょっと不便で / もっとゆっくりお話したくて" 之类
- 引流话术每次重写, 不要复用同一句
- 末尾加"無理しないでね / よかったら"等关心语
"""
            # Phase-4: A/B variant 风格分流
            ab_seg = _bot_referral_ab_segment(cs_ab_variant)
            if ab_seg:
                prompt += "\n" + ab_seg

        prompt += "\n直接输出回复消息，不要加引号、前缀或任何解释。"
        return prompt

    # ─── 引流时机评估 ───
    def _assess_referral_readiness(
        self, context: List[Dict], stage: str,
        last_msg: str, profile: UserProfile,
    ) -> float:
        score = 0.0

        stage_scores = {
            "icebreak": 0.0, "rapport": 0.15, "qualify": 0.35,
            "soft_pitch": 0.55, "referral": 0.85, "follow_up": 0.2,
            "cool_down": 0.0,
        }
        score += stage_scores.get(stage, 0)

        user_msgs = [m for m in context if m.get("role") == "user"]
        if len(user_msgs) >= 3:
            score += 0.1
        if len(user_msgs) >= 5:
            score += 0.05

        # 通讯录好友加速
        if profile.source == "contact":
            score += 0.15

        # 高意向关键词
        if last_msg:
            lower = last_msg.lower()
            intent_words = ["interested", "how", "tell me", "contact",
                            "telegram", "whatsapp", "dimmi", "come",
                            "voglio", "contatto", "gruppo"]
            if any(w in lower for w in intent_words):
                score += 0.2

        return min(1.0, score)

    # ─── 清洗回复 ───
    def _clean_reply(self, text: str) -> str:
        if not text:
            return ""
        text = text.strip()
        # 去掉 AI 常见的前缀/引号
        for prefix in ['"', "'", "Reply:", "回复:", "Message:", "消息:"]:
            if text.startswith(prefix):
                text = text[len(prefix):]
        for suffix in ['"', "'"]:
            if text.endswith(suffix):
                text = text[:-len(suffix)]
        return text.strip()

    # ─── 自动摘要 ───
    def _auto_summarize(self, lead_id: str, platform: str = ""):
        try:
            all_msgs = self._memory.get_all_messages(lead_id, platform)
            if len(all_msgs) < 30:
                return
            text_parts = []
            for m in all_msgs[-50:]:
                role = m.get("role", "?")
                content = m.get("content", "")
                text_parts.append(f"{role}: {content}")
            conversation_text = "\n".join(text_parts)

            prompt = (
                "请用 2-3 句话总结以下对话的关键信息，包括：对方的身份/兴趣、"
                "对话进展到什么阶段、对方对引流的态度。"
                f"\n\n{conversation_text}"
            )
            summary = self._llm.chat_messages(
                [{"role": "user", "content": prompt}], max_tokens=200,
            )
            if summary:
                self._memory.set_summary(lead_id, summary, len(all_msgs))
                log.info(f"ChatBrain: 已为 {lead_id} 生成对话摘要")
        except Exception as e:
            log.warning(f"ChatBrain: 摘要生成失败: {e}")

    # ─── 更新画像 ───
    def update_profile(self, lead_id: str, profile: UserProfile):
        self._profiles[lead_id] = profile

    def get_profile(self, lead_id: str) -> Optional[UserProfile]:
        return self._profiles.get(lead_id)

    # ─── 获取对话历史（供 Dashboard 展示）───
    def get_conversation(self, lead_id: str, limit: int = 50) -> List[Dict]:
        return self._memory.get_all_messages(lead_id)[:limit]

    def get_conversation_stats(self, lead_id: str) -> Dict:
        msgs = self._memory.get_all_messages(lead_id)
        user_msgs = [m for m in msgs if m.get("role") == "user"]
        asst_msgs = [m for m in msgs if m.get("role") == "assistant"]
        last_stage = ""
        for m in reversed(msgs):
            meta = m.get("metadata", "{}")
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except:
                    meta = {}
            if meta.get("stage"):
                last_stage = meta["stage"]
                break
        return {
            "total_messages": len(msgs),
            "user_messages": len(user_msgs),
            "assistant_messages": len(asst_msgs),
            "rounds": min(len(user_msgs), len(asst_msgs)),
            "current_stage": last_stage,
            "last_active": msgs[-1].get("timestamp", 0) if msgs else 0,
        }
