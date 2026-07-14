# -*- coding: utf-8 -*-
"""
ProfileAnalyzer — 用户画像引擎：在首次接触前分析目标用户。

两种分析模式:
1. 文本分析: 从 username + bio 推断行业/兴趣/性格 (快速, 无 API 调用)
2. Vision 分析: 从资料页截图深度分析 (精准, 消耗 Vision API 额度)

使用方式:
    analyzer = ProfileAnalyzer.get_instance()
    profile = analyzer.analyze_text(username='marco_racing', bio='🏎 Car lover | Modding')
    # 或
    profile = await analyzer.analyze_screenshot(screenshot_path, username)
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from .chat_brain import UserProfile
from .llm_client import get_llm_client

log = logging.getLogger(__name__)


# ─── 规则引擎：零 API 调用的快速画像 ───

# 行业关键词映射
_INDUSTRY_KEYWORDS = {
    "automotive": ["car", "auto", "racing", "motor", "vehicle", "drift", "tuning",
                    "macchina", "auto", "motore", "coche"],
    "fashion": ["fashion", "style", "outfit", "model", "beauty", "moda", "stile",
                "makeup", "cosmetic"],
    "fitness": ["fitness", "gym", "workout", "training", "sport", "palestra",
                "yoga", "crossfit", "body"],
    "food": ["food", "cook", "chef", "recipe", "restaurant", "cucina", "cibo",
             "pizza", "pasta"],
    "tech": ["tech", "code", "developer", "software", "crypto", "nft", "ai",
             "blockchain", "web3"],
    "travel": ["travel", "wander", "explore", "adventure", "viaggio", "trip",
               "backpack", "nomad"],
    "music": ["music", "dj", "producer", "singer", "band", "musica", "rapper",
              "beat"],
    "art": ["art", "design", "creative", "illustr", "photo", "film", "video",
            "arte", "disegno"],
    "business": ["business", "entrepreneur", "ceo", "founder", "startup",
                 "marketing", "coach", "mentor", "imprenditore"],
    "gaming": ["gamer", "gaming", "esport", "twitch", "stream", "play"],
    "real_estate": ["real estate", "property", "immobil", "casa", "house",
                     "invest"],
    "education": ["teacher", "professor", "university", "student", "study",
                   "learn"],
}

# 性格线索
_PERSONALITY_SIGNALS = {
    "extrovert": ["🎉", "🔥", "💪", "party", "love", "friends", "social",
                   "fun", "crazy", "wild"],
    "introvert": ["📚", "🎵", "quiet", "peace", "thoughts", "soul", "deep",
                   "minimal", "simple"],
}

# 账号类型线索
_ACCOUNT_TYPE_SIGNALS = {
    "creator": ["content creator", "influencer", "blogger", "youtuber",
                "tiktoker", "creator"],
    "business": ["shop", "store", "brand", "official", "company", "agency",
                 "service", "vendita", "negozio"],
    "personal": [],  # 默认
}


class ProfileAnalyzer:
    """用户画像分析器"""

    _instance: Optional["ProfileAnalyzer"] = None

    def __init__(self):
        self._llm = None  # 延迟加载

    @classmethod
    def get_instance(cls) -> "ProfileAnalyzer":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def analyze_text(
        self,
        username: str = "",
        bio: str = "",
        follower_count: int = 0,
        following_count: int = 0,
        source: str = "follow",
        extra_data: Optional[Dict] = None,
    ) -> UserProfile:
        """
        规则引擎快速分析（零 API 调用）。
        从 username + bio 推断行业、兴趣、性格、账号类型。
        """
        text = f"{username} {bio}".lower()

        # 行业检测
        industry = ""
        industry_score = 0
        for ind, keywords in _INDUSTRY_KEYWORDS.items():
            score = sum(1 for k in keywords if k in text)
            if score > industry_score:
                industry = ind
                industry_score = score

        # 兴趣提取
        interests = []
        for ind, keywords in _INDUSTRY_KEYWORDS.items():
            matches = [k for k in keywords if k in text]
            if matches:
                interests.append(ind)
        interests = interests[:5]

        # 性格推断
        personality = "neutral"
        ext_score = sum(1 for s in _PERSONALITY_SIGNALS["extrovert"] if s in text)
        int_score = sum(1 for s in _PERSONALITY_SIGNALS["introvert"] if s in text)
        if ext_score > int_score:
            personality = "extrovert"
        elif int_score > ext_score:
            personality = "introvert"

        # 账号类型
        account_type = "personal"
        for atype, signals in _ACCOUNT_TYPE_SIGNALS.items():
            if any(s in text for s in signals):
                account_type = atype
                break

        # 语言风格推断
        emoji_count = len(re.findall(r'[\U0001f600-\U0001f650\U0001f300-\U0001f5ff'
                                     r'\U0001f900-\U0001f9ff]', bio or ""))
        language_style = "casual"
        if emoji_count >= 3:
            language_style = "emoji_heavy"
        elif any(w in text for w in ["ceo", "founder", "official", "professional"]):
            language_style = "formal"

        # 破冰话题推荐
        icebreaker_topics = self._suggest_topics(industry, interests, bio, username)

        # 引流切入角度
        referral_angle = self._suggest_referral_angle(industry, interests, account_type)

        return UserProfile(
            username=username,
            bio=bio or "",
            industry=industry,
            interests=interests,
            personality=personality,
            account_type=account_type,
            follower_count=follower_count,
            following_count=following_count,
            language_style=language_style,
            icebreaker_topics=icebreaker_topics,
            referral_angle=referral_angle,
            source=source,
            raw_data=extra_data or {},
        )

    def analyze_with_llm(
        self,
        username: str = "",
        bio: str = "",
        follower_count: int = 0,
        source: str = "follow",
    ) -> UserProfile:
        """
        LLM 深度分析（消耗 1 次 API 调用）。
        对规则引擎结果不足时使用。
        """
        if not self._llm:
            self._llm = get_llm_client()

        # 先用规则引擎兜底
        base = self.analyze_text(username, bio, follower_count, source=source)

        # bio 太短时 LLM 也分析不出什么
        if not bio or len(bio) < 10:
            return base

        prompt = f"""分析这个社交媒体用户的资料，输出 JSON（不要 markdown 代码块）：
用户名: {username}
简介: {bio}
粉丝数: {follower_count}

输出格式:
{{"industry":"行业","interests":["兴趣1","兴趣2"],"personality":"extrovert/introvert/neutral","account_type":"creator/business/personal","icebreaker_topics":["话题1","话题2"],"referral_angle":"引流切入角度"}}"""

        try:
            resp = self._llm.chat_messages(
                [{"role": "user", "content": prompt}], max_tokens=200,
            )
            data = json.loads(resp.strip().strip("`").strip())
            if data.get("industry"):
                base.industry = data["industry"]
            if data.get("interests"):
                base.interests = data["interests"][:5]
            if data.get("personality"):
                base.personality = data["personality"]
            if data.get("account_type"):
                base.account_type = data["account_type"]
            if data.get("icebreaker_topics"):
                base.icebreaker_topics = data["icebreaker_topics"][:3]
            if data.get("referral_angle"):
                base.referral_angle = data["referral_angle"]
        except Exception as e:
            log.warning(f"ProfileAnalyzer LLM 分析失败，使用规则引擎结果: {e}")

        return base

    def _suggest_topics(
        self, industry: str, interests: List[str],
        bio: str, username: str,
    ) -> List[str]:
        """基于画像推荐破冰话题"""
        topics = []

        topic_map = {
            "automotive": ["你主页的车太帅了", "你改装过车吗", "你喜欢什么品牌"],
            "fashion": ["你的穿搭风格很好看", "这个搭配在哪买的", "你有自己的品牌吗"],
            "fitness": ["你练了多久了", "你的训练计划是什么", "推荐什么补剂"],
            "food": ["这道菜看起来太棒了", "你是专业厨师吗", "你最拿手的菜是什么"],
            "tech": ["你在做什么项目", "你觉得AI会怎样发展", "你用什么编程语言"],
            "travel": ["这是在哪拍的", "你最推荐去哪", "你下一站去哪"],
            "music": ["你的音乐风格是什么", "你有在哪里发布作品吗", "你最近在听什么"],
            "business": ["你的公司做什么的", "你是怎么开始创业的", "你的目标市场是哪里"],
            "gaming": ["你在玩什么游戏", "你的段位是多少", "你有直播吗"],
            "real_estate": ["你做哪个区域的", "现在市场怎么样", "你专注什么类型的房产"],
        }

        if industry and industry in topic_map:
            topics.extend(topic_map[industry][:2])

        if not topics:
            topics.append("你的主页内容很有趣")
            if bio:
                topics.append(f"看到你简介说'{bio[:20]}...'")

        return topics[:3]

    def _suggest_referral_angle(
        self, industry: str, interests: List[str], account_type: str,
    ) -> str:
        """建议引流切入角度"""
        angle_map = {
            "automotive": "车友社群",
            "fashion": "时尚买手群",
            "fitness": "健身打卡群",
            "food": "美食分享群",
            "tech": "技术交流群",
            "travel": "旅行搭子群",
            "music": "音乐人社群",
            "business": "商业资源对接群",
            "gaming": "游戏开黑群",
            "real_estate": "投资交流群",
        }

        if industry and industry in angle_map:
            return angle_map[industry]

        if account_type == "business":
            return "行业资源群"
        elif account_type == "creator":
            return "创作者交流群"
        return "兴趣交流群"
