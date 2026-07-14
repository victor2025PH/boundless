# -*- coding: utf-8 -*-
"""
ChatBridge — ChatBrain 与 TikTok 自动化主链路的桥接模块。

提供与旧接口兼容的函数签名，内部使用 ChatBrain 生成回复。
旧路径 (AutoReply + 模板) 作为降级方案保留。

集成点:
  1. check_and_chat_followbacks → generate_followback_message()
  2. check_inbox._handle_inbox_message → generate_inbox_reply()
  3. _build_referral_reply → generate_referral()
  4. 通讯录好友聊天 → generate_contact_message()
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .chat_brain import ChatBrain, ChatResult, UserProfile
from .profile_analyzer import ProfileAnalyzer

log = logging.getLogger(__name__)


def _get_brain() -> ChatBrain:
    return ChatBrain.get_instance()


def _get_analyzer() -> ProfileAnalyzer:
    return ProfileAnalyzer.get_instance()


def _build_profile(
    username: str,
    bio: str = "",
    follower_count: int = 0,
    source: str = "follow",
    use_llm: bool = False,
) -> UserProfile:
    """构建用户画像，优先规则引擎，可选 LLM 加深"""
    analyzer = _get_analyzer()
    if use_llm and bio and len(bio) >= 10:
        return analyzer.analyze_with_llm(username, bio, follower_count, source)
    return analyzer.analyze_text(username, bio, follower_count, source=source)


# ─── 1. 回关聊天：替代 check_and_chat_followbacks 中的消息生成 ───

def generate_followback_message(
    username: str,
    bio: str = "",
    follower_count: int = 0,
    target_language: str = "",
    contact_info: str = "",
    device_id: str = "",
) -> Optional[str]:
    """
    为回关用户生成个性化破冰消息。
    替代旧的: tiktok_chat_ai.generate_personalized_dm_from_profile()
    """
    try:
        profile = _build_profile(username, bio, follower_count, source="follow")
        brain = _get_brain()
        result = brain.generate_icebreaker(
            lead_id=username,
            profile=profile,
            platform="tiktok",
            target_language=target_language,
            source="follow",
        )
        if result.message:
            log.info(f"[ChatBridge] 回关消息生成成功: {username} "
                     f"stage={result.stage} quality={result.quality_score:.1f}")
            return result.message
    except Exception as e:
        log.warning(f"[ChatBridge] 回关消息生成失败: {e}")
    return None


# ─── 2. 收件箱回复：替代 _generate_contextual_reply + _generate_chat_message ───

def generate_inbox_reply(
    username: str,
    incoming_message: str,
    conversation_context: Optional[List[Dict]] = None,
    bio: str = "",
    target_language: str = "",
    contact_info: str = "",
    device_id: str = "",
    conv_state: str = "",
) -> Optional[str]:
    """
    为收件箱消息生成 AI 回复。
    替代旧的: _generate_contextual_reply() + 模板降级链路
    """
    try:
        profile = _build_profile(username, bio, source="inbox")
        brain = _get_brain()

        # 如果有旧上下文但 ChatBrain 记忆为空，先注入
        if conversation_context:
            mem = brain._memory
            if mem.get_message_count(username) == 0:
                for ctx in conversation_context:
                    role = ctx.get("role", "user")
                    text = ctx.get("text") or ctx.get("content", "")
                    if text:
                        mem.add_message(username, role, text, platform="tiktok")

        result = brain.generate_reply(
            lead_id=username,
            incoming_message=incoming_message,
            profile=profile,
            platform="tiktok",
            target_language=target_language,
            contact_info=contact_info,
            source="inbox",
        )

        if result.message:
            log.info(f"[ChatBridge] 收件箱回复生成: {username} "
                     f"stage={result.stage} ref_score={result.referral_score:.2f}")
            return result.message
    except Exception as e:
        log.warning(f"[ChatBridge] 收件箱回复失败: {e}")
    return None


# ─── 3. 引流消息：替代 _build_referral_reply ───

def generate_referral(
    username: str,
    contact_info: str,
    bio: str = "",
    target_language: str = "",
    conversation_context: Optional[List[Dict]] = None,
) -> Optional[str]:
    """
    生成自然的引流消息（引导到 TG/WA）。
    替代旧的: _build_referral_reply() + 模板
    """
    try:
        profile = _build_profile(username, bio, source="referral")
        brain = _get_brain()

        # 注入旧上下文
        if conversation_context:
            mem = brain._memory
            if mem.get_message_count(username) == 0:
                for ctx in conversation_context:
                    role = ctx.get("role", "user")
                    text = ctx.get("text") or ctx.get("content", "")
                    if text:
                        mem.add_message(username, role, text, platform="tiktok")

        # 强制到 referral 阶段
        result = brain.generate_reply(
            lead_id=username,
            incoming_message="",
            profile=profile,
            platform="tiktok",
            target_language=target_language,
            contact_info=contact_info,
            source="referral",
        )

        if result.message:
            log.info(f"[ChatBridge] 引流消息生成: {username} → {contact_info[:20]}...")
            return result.message
    except Exception as e:
        log.warning(f"[ChatBridge] 引流消息生成失败: {e}")
    return None


# ─── 4. 通讯录好友：全新功能 ───

def generate_contact_message(
    username: str,
    phone_number: str = "",
    bio: str = "",
    target_language: str = "",
    contact_info: str = "",
) -> Optional[str]:
    """
    为通讯录好友生成个性化消息。
    这类用户是"温线索"，策略更直接。
    """
    try:
        profile = _build_profile(username, bio, source="contact")
        brain = _get_brain()

        result = brain.generate_icebreaker(
            lead_id=username,
            profile=profile,
            platform="tiktok",
            target_language=target_language,
            source="contact",
        )

        if result.message:
            log.info(f"[ChatBridge] 通讯录好友消息: {username} "
                     f"stage={result.stage}")
            return result.message
    except Exception as e:
        log.warning(f"[ChatBridge] 通讯录好友消息失败: {e}")
    return None


# ─── 5. 查询接口（供 Dashboard 使用）───

def get_lead_conversation(lead_id: str, limit: int = 50) -> List[Dict]:
    """获取完整对话历史"""
    return _get_brain().get_conversation(lead_id, limit)


def get_lead_stats(lead_id: str) -> Dict:
    """获取对话统计"""
    return _get_brain().get_conversation_stats(lead_id)


def get_lead_profile(lead_id: str) -> Optional[Dict]:
    """获取缓存的用户画像"""
    p = _get_brain().get_profile(lead_id)
    if p:
        return {
            "username": p.username, "bio": p.bio, "industry": p.industry,
            "interests": p.interests, "personality": p.personality,
            "account_type": p.account_type, "referral_angle": p.referral_angle,
            "source": p.source, "language_style": p.language_style,
        }
    return None
