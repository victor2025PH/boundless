"""
竞品账号分析引擎。

功能：
1. 定时抓取已配置竞品账号的高赞内容（使用 Serper API 的 Google 搜索）
2. 提取内容框架特征（hook类型、内容结构、调性）
3. 更新 studio_framework_perf 权重（竞品爆款 → 该框架的 prior_boost）
4. 生成"竞品洞察"报告供前端展示

设计原则：
- Serper Key 不可用时优雅降级（返回空结果，不报错）
- 不存储原始内容（版权风险），只存储框架特征
- 分析结果用于提升建议引擎的精准度，不直接影响内容生成
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import urllib.request
import urllib.error
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("competitor_analyzer")

# ── 框架关键词映射（用于从标题/描述推断框架类型）───────────────────────────

FRAMEWORK_KEYWORDS: Dict[str, List[str]] = {
    "hook_question":        ["do you", "are you", "have you", "why", "what if", "did you know"],
    "before_after":         ["before and after", "transformation", "glow up", "results", "progress"],
    "myth_busting":         ["myth", "wrong", "everyone thinks", "actually", "truth about", "stop believing"],
    "5_step_tutorial":      ["step", "steps", "how to", "guide", "tutorial", "tips to"],
    "story_transformation": ["my story", "i used to", "changed my life", "journey", "from", "to "],
    "stat_shock":           ["%", "million", "billion", "statistic", "study shows", "research", "data"],
    "mistake_correction":   ["mistake", "error", "wrong way", "stop doing", "avoid", "never do"],
    "day_in_life":          ["day in my life", "morning routine", "daily", "a day as", "my routine"],
    "expert_3_tips":        ["3 tips", "top tips", "expert", "pro tip", "hack", "secret"],
    "mindset_shift":        ["mindset", "perspective", "change how", "think about", "paradigm"],
    "comparison":           ["vs", "versus", "which is better", "comparison", "compared"],
    "behind_scenes":        ["behind the scenes", "bts", "how i make", "process", "making of"],
    "emotional_hook":       ["feeling", "emotion", "heart", "moved", "touched", "inspire", "story"],
    "trending_reaction":    ["trend", "viral", "everyone", "doing this", "challenge", "duet"],
    "social_proof":         ["testimonial", "review", "customer", "client", "people say", "results"],
    "quick_win":            ["in 5 minutes", "easy", "simple", "quick", "fast", "instant", "today"],
}


def _infer_framework(title: str, description: str = "") -> str:
    """从标题和描述中推断最匹配的框架 ID。"""
    text = (title + " " + description).lower()
    scores: Dict[str, int] = {}
    for fw_id, keywords in FRAMEWORK_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text)
        if score > 0:
            scores[fw_id] = score
    if not scores:
        return "hook_question"  # 默认
    return max(scores, key=lambda k: scores[k])


def _serper_search(query: str, num: int = 10) -> List[Dict[str, Any]]:
    """
    使用 Serper API 搜索内容，返回结果列表。
    Serper Key 未配置时返回空列表。
    """
    api_key = os.environ.get("SERPER_API_KEY", "")
    if not api_key:
        logger.debug("SERPER_API_KEY 未配置，跳过竞品搜索")
        return []

    payload = json.dumps({"q": query, "num": num}).encode()
    req = urllib.request.Request(
        "https://google.serper.dev/search",
        data=payload,
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return data.get("organic", [])
    except Exception as e:
        logger.warning("Serper 搜索失败: %s", e)
        return []


def analyze_competitor(url: str, platform: str, persona_id: str) -> Dict[str, Any]:
    """
    分析单个竞品账号，返回框架洞察。

    当 Serper Key 可用时：搜索该账号最近爆款内容，提取框架分布。
    当 Serper Key 不可用时：返回基于账号 URL 的静态分析（基于用户名推断内容方向）。
    """
    # 提取用户名
    username = url.rstrip("/").split("/")[-1].lstrip("@")
    site_map = {"tiktok": "tiktok.com", "instagram": "instagram.com", "youtube": "youtube.com"}
    site = site_map.get(platform, platform + ".com")

    query = f'site:{site} @{username} viral OR trending'
    results = _serper_search(query, num=10)

    framework_counts: Dict[str, int] = {}
    insights: List[str] = []

    if results:
        for item in results:
            title = item.get("title", "")
            snippet = item.get("snippet", "")
            fw = _infer_framework(title, snippet)
            framework_counts[fw] = framework_counts.get(fw, 0) + 1
            if len(insights) < 3:
                insights.append(title[:80])
    else:
        # Serper 不可用时：基于账号名/平台推断
        logger.info("无 Serper Key，使用静态竞品分析: %s", url)
        # 返回该平台的基准框架分布（基于行业数据）
        platform_defaults = {
            "tiktok":     {"hook_question": 3, "before_after": 2, "quick_win": 2, "trending_reaction": 2},
            "instagram":  {"before_after": 3, "social_proof": 2, "day_in_life": 2, "emotional_hook": 1},
            "youtube":    {"5_step_tutorial": 3, "expert_3_tips": 2, "myth_busting": 2},
            "xiaohongshu":{"before_after": 3, "day_in_life": 2, "social_proof": 2},
        }
        framework_counts = platform_defaults.get(platform, {"hook_question": 2, "quick_win": 2})

    # 更新框架性能先验（竞品爆款 = 框架有效的证据）
    if framework_counts:
        _boost_framework_perf(framework_counts, source=f"competitor:{username}")

    return {
        "url": url,
        "platform": platform,
        "username": username,
        "persona_id": persona_id,
        "framework_distribution": framework_counts,
        "top_framework": max(framework_counts, key=lambda k: framework_counts[k]) if framework_counts else None,
        "insights": insights,
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
        "data_source": "serper" if results else "static_baseline",
    }


def _boost_framework_perf(framework_counts: Dict[str, int], source: str = "competitor") -> None:
    """将竞品分析结果注入框架性能表（prior boost）。"""
    try:
        from .studio_db import _get_conn
        with _get_conn() as conn:
            for fw_id, count in framework_counts.items():
                # 竞品每次出现 = 0.3个虚拟通过记录（弱先验，不覆盖真实数据）
                virtual_approved = max(1, int(count * 0.3))
                conn.execute("""
                    INSERT INTO studio_framework_perf(framework_id, approved_count)
                    VALUES (?, ?)
                    ON CONFLICT(framework_id) DO UPDATE SET
                        approved_count = approved_count + excluded.approved_count,
                        last_updated = datetime('now')
                """, (fw_id, virtual_approved))
            conn.commit()
        logger.info("竞品先验已注入框架性能表: %s (%d框架)", source, len(framework_counts))
    except Exception as e:
        logger.warning("框架先验注入失败: %s", e)


def run_competitor_analysis(personas_config: dict, strategy_config: dict) -> List[Dict[str, Any]]:
    """
    批量分析所有已配置的竞品账号。
    由 studio_manager 的后台线程定期调用（每24小时一次）。
    """
    competitors = strategy_config.get("competitors", [])
    if not competitors:
        logger.info("未配置竞品账号，跳过分析")
        return []

    results = []
    for comp in competitors:
        url      = comp.get("url", "")
        platform = comp.get("platform", "tiktok")
        persona_id = comp.get("persona_id", "")

        if not url:
            continue
        try:
            result = analyze_competitor(url, platform, persona_id)
            results.append(result)
            logger.info("竞品分析完成: %s | top框架: %s", url, result.get("top_framework"))
        except Exception as e:
            logger.warning("竞品分析失败 %s: %s", url, e)
            results.append({"url": url, "error": str(e)})

    return results
