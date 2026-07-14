# -*- coding: utf-8 -*-
"""
Facebook 帖子内容分析器 — 规则层 (v1, 不依赖 LLM)。

职责:
  1. 对帖子内容做关键词匹配 → 话题分类 + 内容得分
  2. 结合 L1 名字得分 → 综合评分 → A/B/C 分层
  3. 跨设备错峰查询 (48h 内是否已有其他设备互动过同一作者)

设计原则 (批判评审共识):
  - 高精度低召回: 宁可漏掉好帖子, 不要评论到商业帖/男性帖
  - 名字 L1 为主判定, 内容为加分项
  - v1 只跑 B 类 (点赞+加友), A 类评论待数据验证后开放
"""
from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ─── 正向关键词 → 话题分类 ───────────────────────────────────────────

POSITIVE_TOPICS: Dict[str, Dict[str, Any]] = {
    "self_intro": {
        "keywords": [
            "はじめまして", "自己紹介", "よろしく", "初めて投稿",
            "初投稿", "新参者", "初めまして",
        ],
        "score": 35,
        "label": "自己紹介",
    },
    "parenting": {
        "keywords": [
            "子育て", "育児", "ママ", "幼稚園", "小学校", "運動会",
            "子供", "息子", "娘", "赤ちゃん", "保育園", "お弁当",
            "PTA", "学校", "卒業", "入学", "参観日", "ムスメ", "ムスコ",
        ],
        "score": 30,
        "label": "子育て",
    },
    "lifestyle": {
        "keywords": [
            "日常", "暮らし", "生活", "料理", "家事", "掃除",
            "ランチ", "カフェ", "お出かけ", "買い物", "断捨離",
            "片付け", "節約", "時短",
        ],
        "score": 25,
        "label": "日常生活",
    },
    "hobby": {
        "keywords": [
            "趣味", "手芸", "ガーデニング", "旅行", "読書",
            "ヨガ", "散歩", "写真", "映画", "韓ドラ", "推し",
            "ハンドメイド", "編み物", "お花", "アロマ", "パン作り",
            "ネイル", "コスメ",
        ],
        "score": 25,
        "label": "趣味",
    },
    "health": {
        "keywords": [
            "健康", "更年期", "ダイエット", "運動", "ウォーキング",
            "睡眠", "体調", "漢方", "ストレッチ", "冷え性",
        ],
        "score": 20,
        "label": "健康",
    },
    "emotional": {
        "keywords": [
            "感謝", "ありがとう", "嬉しい", "頑張", "元気",
            "応援", "癒し", "笑顔", "幸せ", "楽しい",
        ],
        "score": 15,
        "label": "気持ち",
    },
}

# ─── 負向关键词 → 排除 ────────────────────────────────────────────────

NEGATIVE_TOPICS: Dict[str, Dict[str, Any]] = {
    "commercial": {
        "keywords": [
            "販売", "セミナー", "ビジネス", "副業", "投資",
            "稼ぐ", "収入", "LINE@", "無料相談", "セッション",
            "コンサル", "募集中", "お申し込み", "モニター",
            "限定", "先着", "残席", "特別価格", "期間限定",
            "公式LINE", "メルマガ", "登録",
        ],
        "score": -40,
        "label": "商業推広",
    },
    "male_topic": {
        "keywords": [
            "釣り", "パチンコ", "競馬", "野球", "サッカー観戦",
            "筋トレ", "プロテイン", "車", "バイク", "ゴルフ",
        ],
        "score": -20,
        "label": "男性話題",
    },
    "spam": {
        "keywords": [
            "http://", "https://", "bit.ly", "lin.ee",
            "クリック", "今すぐ", "無料プレゼント",
        ],
        "score": -50,
        "label": "スパム",
    },
}


def score_post_content(text: str) -> Tuple[float, str, str]:
    """规则层内容评分。

    Returns:
        (content_score, topic_key, topic_label)
        content_score 可以是负数 (负向命中)
    """
    if not text or len(text) < 6:
        return (0.0, "", "")

    best_score = 0.0
    best_topic = ""
    best_label = ""

    # 正向匹配 (取最高命中)
    for topic_key, cfg in POSITIVE_TOPICS.items():
        hit = sum(1 for kw in cfg["keywords"] if kw in text)
        if hit > 0:
            # 多关键词命中时有轻微加成
            s = cfg["score"] + min(hit - 1, 3) * 3
            if s > best_score:
                best_score = s
                best_topic = topic_key
                best_label = cfg["label"]

    # 负向匹配 (任一命中即扣分, 可覆盖正向)
    neg_score = 0.0
    neg_topic = ""
    neg_label = ""
    for topic_key, cfg in NEGATIVE_TOPICS.items():
        hit = sum(1 for kw in cfg["keywords"] if kw in text)
        if hit > 0:
            s = cfg["score"]  # 负数
            neg_score += s  # 累积负向 (多类负向同时命中扣更多)
            if not neg_topic:
                neg_topic = topic_key
                neg_label = cfg["label"]

    # 如果负向命中严重, 覆盖正向
    if neg_score < -30 or (neg_score < 0 and best_score < 20):
        return (neg_score, neg_topic, neg_label)

    return (best_score, best_topic, best_label)


def analyze_post(author_name: str, post_text: str,
                 l1_score: float,
                 tier_a_threshold: float = 60,
                 tier_b_threshold: float = 30) -> Dict[str, Any]:
    """分析单条帖子, 返回综合评分和分层。

    Args:
        author_name: 帖子作者名
        post_text: 帖子正文
        l1_score: 已经算好的 L1 名字评分
        tier_a_threshold: A 类阈值
        tier_b_threshold: B 类阈值

    Returns:
        {
            "author_name": str,
            "l1_score": float,
            "content_score": float,
            "total_score": float,
            "tier": "A" | "B" | "C",
            "topic": str,          # 英文 topic key
            "topic_label": str,    # 日文 topic label
        }
    """
    content_score, topic, topic_label = score_post_content(post_text)
    total = l1_score + content_score

    if total >= tier_a_threshold:
        tier = "A"
    elif total >= tier_b_threshold:
        tier = "B"
    else:
        tier = "C"

    return {
        "author_name": author_name,
        "l1_score": l1_score,
        "content_score": content_score,
        "total_score": total,
        "tier": tier,
        "topic": topic,
        "topic_label": topic_label,
    }


# ─── 跨设备错峰查询 ──────────────────────────────────────────────────

try:
    from src.host.device_registry import data_file as _data_file
    _DB_PATH = _data_file("openclaw.db")
except ImportError:
    _DB_PATH = Path("data/openclaw.db")


def is_author_recently_engaged(author_name: str, group_name: str,
                                current_device: str,
                                cooldown_hours: int = 4) -> bool:
    """查询是否有其他设备在 cooldown_hours 内已互动过此作者。"""
    if not _DB_PATH.exists():
        return False
    conn = None
    try:
        conn = sqlite3.connect(str(_DB_PATH), timeout=5)
        row = conn.execute(
            "SELECT 1 FROM fb_post_engagements "
            "WHERE author_name = ? AND group_name = ? "
            "  AND device_id != ? "
            "  AND created_at > datetime('now', ?)"
            " LIMIT 1",
            (author_name, group_name, current_device,
             f"-{cooldown_hours} hours"),
        ).fetchone()
        return row is not None
    except Exception as e:
        logger.debug("[post_analyzer] DB query error: %s", e)
        return False
    finally:
        if conn:
            conn.close()


def record_engagement(device_id: str, group_name: str,
                      analysis: Dict[str, Any],
                      actions: Dict[str, Any]) -> Optional[int]:
    """写入互动记录到 fb_post_engagements。返回 row id。"""
    conn = None
    try:
        conn = sqlite3.connect(str(_DB_PATH), timeout=5)
        cur = conn.execute(
            "INSERT INTO fb_post_engagements "
            "(device_id, group_name, author_name, post_snippet, post_topic,"
            " l1_score, content_score, total_score, tier,"
            " action_liked, action_commented, comment_text,"
            " action_profile_visited, action_friend_sent)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                device_id,
                group_name,
                analysis.get("author_name", ""),
                actions.get("post_snippet", "")[:200],
                analysis.get("topic", ""),
                analysis.get("l1_score", 0),
                analysis.get("content_score", 0),
                analysis.get("total_score", 0),
                analysis.get("tier", ""),
                int(actions.get("liked", False)),
                int(actions.get("commented", False)),
                actions.get("comment_text", "")[:200],
                int(actions.get("profile_visited", False)),
                int(actions.get("friend_sent", False)),
            ),
        )
        conn.commit()
        return cur.lastrowid
    except Exception as e:
        logger.warning("[post_analyzer] record_engagement error: %s", e)
        return None
    finally:
        if conn:
            conn.close()
