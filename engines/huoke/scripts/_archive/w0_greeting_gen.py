# -*- coding: utf-8 -*-
"""
W0-4: 用 AI 生成 100 条日文打招呼话术，写入 greeting_library。

设计原则（来自开发文档批判者讨论结论）:
  - 默认层A: 只引用 bio/长期兴趣，不引近期帖子（防"毛骨悚然"感）
  - 日文敬体（です・ます調）
  - 12-45 文字，禁止表情符号超过2个
  - 禁止外部链接、禁止"投資/副業/LINE/稼ぐ"等关键词
  - 生成 100 条，涵盖多种风格和引导话题

话术分类（各20条）:
  1. 料理/グルメ系（引 bio 含料理兴趣）
  2. 旅行/お出かけ系
  3. 子育て/ライフ系
  4. 趣味/文化系（手芸/韓ドラ/ガーデニング）
  5. 自己紹介/共通点系（最通用）

用法:
  cd d:\mobile-auto-0327\mobile-auto-project
  $env:PYTHONPATH = "$pwd"
  python scripts/w0_greeting_gen.py

输出:
  - data/w0_greeting_library.json（完整记录）
  - 终端打印每条话术及分析
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("w0_greeting")

# 禁词表（合规检查）
FORBIDDEN_WORDS = [
    "投資", "副業", "副収入", "稼ぐ", "儲かる", "儲け",
    "LINE", "ライン", "telegram", "テレグラム", "WhatsApp",
    "http://", "https://", "www.",
    "クリック", "登録", "無料", "プレゼント", "キャンペーン",
]

# 话术风格标签
STYLE_TAGS = ["formal", "casual", "warm", "curious"]

# 按主题分类的 Prompt 设计
TOPIC_CONFIGS = [
    {
        "topic_id": "cooking",
        "topic_zh": "料理/グルメ系",
        "count": 20,
        "context": """
日本の料理や食べ物に興味がある30〜50代の女性へのFacebook挨拶メッセージ。
相手のプロフィールに「料理」「グルメ」「パン」「お菓子」「和食」「cooking」などのキーワードが含まれているシナリオ。
""",
        "examples": [
            "料理がお好きなんですね！私もパン作りが大好きで、最近はクロワッサンに挑戦中です😊",
            "グルメな情報をいつも参考にしています。どのエリアがお気に入りですか？",
        ],
    },
    {
        "topic_id": "travel",
        "topic_zh": "旅行/お出かけ系",
        "count": 20,
        "context": """
旅行やお出かけが好きな30〜50代の日本女性へのFacebook挨拶。
相手の bio に「旅行」「国内旅行」「温泉」「カフェ巡り」などのキーワードがある場面。
""",
        "examples": [
            "旅行がお好きなんですね！私も旅先でのカフェ探しが趣味で、ぜひ情報交換できたら嬉しいです。",
            "温泉旅行に行きたくて情報を探していました。どちらの温泉地がおすすめですか？",
        ],
    },
    {
        "topic_id": "lifestyle",
        "topic_zh": "子育て/ライフ系",
        "count": 20,
        "context": """
子育て中または子育て経験のある40〜50代の日本女性へのFacebook挨拶。
プロフィールに「主婦」「子育て」「ママ」「家族」などのキーワードがある。
自分も子育て経験があり、共感から話しかける設定。
""",
        "examples": [
            "子育てお疲れ様です！私も息子が小さい頃は毎日バタバタでした。今は少し落ち着いてきましたが😊",
            "主婦としての生活、いろいろと共通点がありそうで、ぜひつながれたら嬉しいです。",
        ],
    },
    {
        "topic_id": "hobby",
        "topic_zh": "趣味/文化系",
        "count": 20,
        "context": """
手芸、韓ドラ、ガーデニング、読書、音楽など趣味が豊かな30〜50代日本女性へのFacebook挨拶。
相手の bio に具体的な趣味が書かれている場面。趣味への共感を入口にした自然な声かけ。
""",
        "examples": [
            "韓ドラがお好きなんですね！私も最近『愛の不時着』を見返してはまっています😂",
            "手芸作品がとても素敵ですね。私も刺繍が趣味で、いつも参考にしたいと思っています。",
        ],
    },
    {
        "topic_id": "intro",
        "topic_zh": "自己紹介/共通点系",
        "count": 20,
        "context": """
プロフィールから具体的な趣味がわからない、または汎用的に使える30〜50代日本女性へのFacebook挨拶。
自己紹介ベースで、圧力をかけず、自然に友達申請を受け入れてもらいやすい文章。
同じ地域や似た年代であることを優しく伝えるトーン。
""",
        "examples": [
            "はじめまして！プロフィールを拝見して、共通点がありそうで思わずご連絡しました。よろしければ仲良くしていただけると嬉しいです😊",
            "突然のメッセージ失礼します。同じ年代の方とつながりたくて。よろしくお願いします。",
        ],
    },
]

SYSTEM_PROMPT = """あなたは自然な日本語のFacebookメッセージを書く専門家です。

以下のルールを厳守してください：
- 日本語の敬体（です・ます調）で書く
- 文字数：12〜45文字（厳守）
- 絵文字は0〜2個まで（それ以上は禁止）
- 外部リンクは一切禁止
- 以下のキーワードは絶対に使用禁止：投資、副業、副収入、稼ぐ、儲かる、LINE、テレグラム、登録、無料プレゼント
- 自然で温かみのある文章（セールス感ゼロ）
- 相手への質問または共感を含める
- 初めて連絡する場面として書く

出力形式：
- 1行に1つのメッセージのみを出力
- 番号や記号は不要
- 説明や前置きは不要"""


def call_llm(prompt: str, system: str = SYSTEM_PROMPT, max_tokens: int = 2048) -> str:
    """呼び出し LLM（zhipu GLM-4 via get_llm_client()，读 config/ai.yaml）。"""
    base = Path(__file__).parent.parent
    sys.path.insert(0, str(base))
    from src.ai.llm_client import get_llm_client
    client = get_llm_client()

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]
    try:
        resp = client.chat_messages(messages, max_tokens=max_tokens, temperature=0.85)
        return str(resp) if resp else ""
    except Exception as e:
        log.error("LLM 调用失败: %s", e)
        return ""


def check_compliance(text: str) -> tuple[bool, list[str]]:
    """检查话术合规性。返回 (通过, 违规原因列表)。"""
    issues = []

    # 长度检查
    char_count = len(text.strip())
    if char_count < 12:
        issues.append(f"过短({char_count}字)")
    if char_count > 60:
        issues.append(f"过长({char_count}字)")

    # 禁词
    for w in FORBIDDEN_WORDS:
        if w.lower() in text.lower():
            issues.append(f"含禁词「{w}」")

    # 表情符号计数（简单检测 emoji unicode 范围）
    emoji_count = sum(1 for c in text if ord(c) > 0x1F300)
    if emoji_count > 2:
        issues.append(f"表情符号过多({emoji_count}个)")

    # 必须包含日文
    has_hiragana = bool(re.search(r"[\u3040-\u309F]", text))
    has_katakana = bool(re.search(r"[\u30A0-\u30FF]", text))
    has_cjk = bool(re.search(r"[\u4E00-\u9FFF]", text))
    if not (has_hiragana or has_katakana or has_cjk):
        issues.append("不包含日文")

    return len(issues) == 0, issues


def generate_topic_greetings(config: dict) -> list[dict]:
    """为一个话题类别生成打招呼话术。"""
    topic_id = config["topic_id"]
    topic_zh = config["topic_zh"]
    count = config["count"]
    context = config["context"]
    examples = config["examples"]

    log.info("生成「%s」话术 (目标 %d 条)...", topic_zh, count)

    examples_str = "\n".join(f"- {e}" for e in examples)

    prompt = f"""以下のシナリオで、Facebookで初めて連絡するための自然な挨拶メッセージを{count}個作成してください。

シナリオ:
{context.strip()}

参考例（このスタイルを参考に、コピーせず新しいものを作る）:
{examples_str}

注意事項:
- {count}個、すべて異なる表現で作成すること
- 各メッセージは1行で出力（空行で区切らない）
- 文体のバリエーション: 丁寧・自然・共感的・好奇心旺盛などを混ぜる
- 相手の趣味やプロフィールに自然に触れる文体

{count}個のメッセージを今すぐ出力してください："""

    raw = call_llm(prompt, max_tokens=3000)
    if not raw:
        log.warning("LLM 返回空内容")
        return []

    # 解析每行
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    # 去掉编号（如 "1. " "- " "・" 等前缀）
    cleaned = []
    for line in lines:
        line = re.sub(r"^[\d]+[\.．\)）\s]+", "", line)
        line = re.sub(r"^[-・•★●▶]\s*", "", line)
        line = line.strip().strip("「」『』")
        if line and len(line) >= 10:
            cleaned.append(line)

    # 合规过滤
    valid = []
    for text in cleaned:
        ok, issues = check_compliance(text)
        if ok:
            valid.append({
                "topic_id": topic_id,
                "topic_zh": topic_zh,
                "text_ja": text,
                "reference_layer": "A",
                "style_tag": _guess_style(text),
                "char_count": len(text),
                "compliance_ok": True,
            })
        else:
            log.debug("合规失败: 「%s」 — %s", text[:40], issues)

    log.info("  生成 %d 行 → 合规 %d 条", len(cleaned), len(valid))
    return valid[:count]  # 不超过目标数量


def _guess_style(text: str) -> str:
    """简单推断话术风格。"""
    if any(w in text for w in ["失礼", "申し訳", "突然"]):
        return "formal"
    if any(w in text for w in ["笑", "！", "✨", "😊", "😂"]):
        return "casual"
    if any(w in text for w in ["嬉しい", "素敵", "共感", "素晴らしい"]):
        return "warm"
    if "？" in text or "ですか" in text or "かしら" in text:
        return "curious"
    return "casual"


def deduplicate(items: list[dict]) -> list[dict]:
    """去重（按 text_ja）。"""
    seen = set()
    result = []
    for item in items:
        t = item["text_ja"]
        if t not in seen:
            seen.add(t)
            result.append(item)
    return result


def main():
    log.info("=== W0-4: 日文打招呼话术生成 ===")

    base_dir = Path(__file__).parent.parent
    out_path = base_dir / "data" / "w0_greeting_library.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    all_greetings = []

    for config in TOPIC_CONFIGS:
        items = generate_topic_greetings(config)
        all_greetings.extend(items)
        log.info("累计: %d 条", len(all_greetings))
        time.sleep(2)  # LLM 限速等待

    # 如果生成不足 100 条，补充通用话术
    if len(all_greetings) < 100:
        log.info("生成数量不足 100，补充通用话术...")
        extra_config = {
            "topic_id": "general",
            "topic_zh": "通用补充",
            "count": 100 - len(all_greetings),
            "context": "汎用的な挨拶メッセージ。相手の趣味が不明な場合でも使えるもの。",
            "examples": [
                "はじめまして！素敵なプロフィールを拝見して、ぜひつながりたいと思いました。",
                "突然のメッセージ失礼します。同じ年代の方とつながりたくて思い切ってご連絡しました😊",
            ],
        }
        extra = generate_topic_greetings(extra_config)
        all_greetings.extend(extra)

    # 去重
    all_greetings = deduplicate(all_greetings)
    log.info("去重后: %d 条", len(all_greetings))

    # 截取 100 条
    final = all_greetings[:100]

    # 写入 JSON
    output = {
        "w0_version": "1.0",
        "generated_at": datetime.now().isoformat(),
        "persona_key": "jp_female_midlife",
        "target_count": 100,
        "actual_count": len(final),
        "greetings": final,
    }
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("话术库已写入: %s", out_path)

    # 打印分析
    _print_greeting_analysis(final)
    return final


def _print_greeting_analysis(greetings: list[dict]):
    """打印话术质量分析报告。"""
    total = len(greetings)
    if total == 0:
        return

    log.info("\n====== W0-4 话术分析报告 ======")
    log.info("总话术数: %d", total)

    # 按话题统计
    from collections import Counter
    topic_cnt = Counter(g["topic_id"] for g in greetings)
    log.info("\n按话题分布:")
    for topic, cnt in topic_cnt.most_common():
        log.info("  %-20s: %d 条", topic, cnt)

    # 按风格统计
    style_cnt = Counter(g.get("style_tag", "unknown") for g in greetings)
    log.info("\n按风格分布:")
    for style, cnt in style_cnt.most_common():
        log.info("  %-10s: %d 条", style, cnt)

    # 字数统计
    lengths = [g["char_count"] for g in greetings]
    log.info("\n字数统计: 最短=%d  最长=%d  平均=%.1f",
             min(lengths), max(lengths), sum(lengths) / len(lengths))

    # 示例展示
    log.info("\n话术示例（每类各1条）:")
    shown_topics = set()
    for g in greetings:
        if g["topic_id"] not in shown_topics:
            log.info("  [%s/%s] 「%s」(%d字)",
                     g["topic_id"], g.get("style_tag", ""),
                     g["text_ja"], g["char_count"])
            shown_topics.add(g["topic_id"])


if __name__ == "__main__":
    main()
